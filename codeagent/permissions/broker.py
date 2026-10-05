"""Permission request brokers for terminal and web runtimes."""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import nullcontext, contextmanager
from contextvars import ContextVar
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from codeagent.runtime.cancellation import CancellationToken
from codeagent.runtime.activity import ExecutionActivity

PermissionPrompt = Callable[[str, dict[str, Any], str], bool]
_execution = ContextVar("permission_execution", default=None)
_tool_id = ContextVar("permission_tool_id", default="")
_stdin_lock = threading.RLock()


@contextmanager
def permission_execution(activity, cancellation, agent_id):
    token = _execution.set((activity, cancellation, agent_id))
    try:
        yield
    finally:
        _execution.reset(token)


@contextmanager
def permission_tool(tool_id):
    token = _tool_id.set(tool_id)
    try:
        yield
    finally:
        _tool_id.reset(token)


class PermissionBroker(Protocol):
    """Runtime-neutral contract for resolving one permission request."""

    def request(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        reason: str,
        *,
        cancellation: CancellationToken | None = None,
        timeout: float | None = None,
    ) -> bool:
        """Return whether the caller may perform the requested operation."""


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """A serializable snapshot of an approval waiting for a decision."""

    id: str
    tool_name: str
    tool_input: dict[str, Any]
    reason: str
    created_at: datetime
    timeout: float | None
    agent_id: str = ""
    tool_use_id: str = ""


def terminal_prompt(tool_name: str, tool_input: dict[str, Any], reason: str) -> bool:
    """Ask for one permission decision on stdin."""

    print()
    print(f"Permission required: {reason}")
    print(f"Tool: {tool_name}({tool_input})")
    choice = input("Allow? [y/N] ").strip().lower()
    return choice in {"y", "yes"}


@dataclass(slots=True)
class CliPermissionBroker:
    """Synchronous broker used by the interactive CLI."""

    prompt: PermissionPrompt = terminal_prompt
    execution_activity: ExecutionActivity | None = None

    def request(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        reason: str,
        *,
        cancellation: CancellationToken | None = None,
        timeout: float | None = None,
    ) -> bool:
        # A terminal input call cannot be interrupted portably. Cancellation is
        # still honored immediately before and after the user interaction.
        del timeout
        binding = _execution.get()
        activity = binding[0] if binding else self.execution_activity
        cancellation = binding[1] if binding else cancellation
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        with (
            activity.operation("approval", float("inf"))
            if activity is not None else nullcontext()
        ):
            with _stdin_lock:
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                allowed = bool(self.prompt(tool_name, dict(tool_input), reason))
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        return allowed


@dataclass(slots=True)
class _PendingRequest:
    request: PermissionRequest
    decision: bool | None = None
    event: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True, slots=True)
class PermissionResult:
    allowed: bool
    reason: str = ""


class WaitingPermissionBroker:
    """Thread-safe broker whose decisions arrive from another runtime thread."""

    def __init__(
        self,
        *,
        default_timeout: float | None = 600.0,
        on_request: Callable[[PermissionRequest], None] | None = None,
        on_timeout: Callable[[PermissionRequest], None] | None = None,
        on_abandon: Callable[[PermissionRequest], None] | None = None,
        poll_interval: float = 0.05,
    ) -> None:
        if default_timeout is not None and default_timeout < 0:
            raise ValueError("default_timeout must be non-negative or None")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._default_timeout = default_timeout
        self._on_request = on_request
        self._on_timeout = on_timeout
        self._on_abandon = on_abandon
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingRequest] = {}
        self.execution_activity: ExecutionActivity | None = None

    @property
    def pending(self) -> tuple[PermissionRequest, ...]:
        """Return active requests in creation order as an immutable snapshot."""

        with self._lock:
            return tuple(item.request for item in self._pending.values())

    def request(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        reason: str,
        *,
        cancellation: CancellationToken | None = None,
        timeout: float | None = None,
    ) -> bool:
        """Publish a request and block until allow, deny, timeout, or cancellation."""
        return self.request_result(tool_name, tool_input, reason, cancellation=cancellation, timeout=timeout).allowed

    def request_result(
        self, tool_name: str, tool_input: dict[str, Any], reason: str, *,
        cancellation: CancellationToken | None = None, timeout: float | None = None,
    ) -> PermissionResult:
        """Preserve the denial cause without sharing mutable state across agents."""
        binding = _execution.get()
        activity = binding[0] if binding else self.execution_activity
        cancellation = binding[1] if binding else cancellation
        with (
            activity.operation("approval", float("inf"))
            if activity is not None else nullcontext()
        ):
            return self._request(
                tool_name, tool_input, reason,
                cancellation=cancellation, timeout=timeout,
            )

    def _request(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        reason: str,
        *,
        cancellation: CancellationToken | None,
        timeout: float | None,
    ) -> PermissionResult:
        if cancellation is not None:
            cancellation.raise_if_cancelled()

        effective_timeout = self._default_timeout if timeout is None else timeout
        if effective_timeout is not None and effective_timeout < 0:
            raise ValueError("timeout must be non-negative or None")

        request = PermissionRequest(
            id=uuid.uuid4().hex,
            tool_name=str(tool_name),
            tool_input=dict(tool_input),
            reason=str(reason),
            created_at=datetime.now(timezone.utc),
            timeout=effective_timeout,
            agent_id=_execution.get()[2] if _execution.get() else "",
            tool_use_id=_tool_id.get(),
        )
        pending = _PendingRequest(request=request)
        with self._lock:
            self._pending[request.id] = pending

        timed_out = False
        try:
            if self._on_request is not None:
                self._on_request(request)

            deadline = (
                None
                if effective_timeout is None
                else time.monotonic() + effective_timeout
            )
            while True:
                if cancellation is not None:
                    cancellation.raise_if_cancelled()

                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    timed_out = True
                    with self._lock:
                        if pending.decision is not None:
                            return PermissionResult(pending.decision, "" if pending.decision else "Permission denied by user")
                        self._pending.pop(request.id, None)
                    if self._on_timeout is not None:
                        self._on_timeout(request)
                    return PermissionResult(False, "审批等待超时，操作未执行；这不是用户拒绝。")
                wait_for = (
                    self._poll_interval
                    if remaining is None
                    else min(self._poll_interval, remaining)
                )
                if not pending.event.wait(wait_for):
                    continue
                # resolve() writes the decision before setting the event.
                return PermissionResult(pending.decision is True, "" if pending.decision else "Permission denied by user")
        finally:
            with self._lock:
                self._pending.pop(request.id, None)
            if pending.decision is None and not timed_out and self._on_abandon is not None:
                self._on_abandon(request)

    def resolve(self, request_id: str, allow: bool, *, before_resolve: Callable[[], None] | None = None) -> bool:
        """Resolve an active request; return ``False`` if it is no longer pending."""

        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return False
            if before_resolve is not None:
                before_resolve()
            self._pending.pop(request_id, None)
            pending.decision = bool(allow)
            pending.event.set()
            return True
