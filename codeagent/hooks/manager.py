"""Separate lifecycle notifications from short-circuit control decisions."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable

HookHandler = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class HookDecision:
    """Synchronous control, in addition to the existing string/None contract."""

    action: str
    message: str = ""
    reason: str = ""
    outcome: str = "blocked"


class HookManager:
    def __init__(self) -> None:
        self._handlers: dict[str, list[HookHandler]] = defaultdict(list)
        self._observers: dict[str, list[HookHandler]] = defaultdict(list)

    def register(self, event: str, handler: HookHandler, *, first: bool = False) -> None:
        """Register a control handler; the first non-None decision wins."""
        if first:
            self._handlers[event].insert(0, handler)
        else:
            self._handlers[event].append(handler)

    def observe(self, event: str, handler: HookHandler) -> None:
        """Register an observer. Return values never control execution."""
        self._observers[event].append(handler)

    @property
    def has_handlers(self) -> bool:
        return any(self._handlers.values()) or any(self._observers.values())

    def copy(
        self, *, rebind: Callable[[HookHandler], HookHandler] | None = None,
        exclude_owner: Any = None,
    ) -> HookManager:
        """Copy registrations so per-Agent guards do not leak to other Agents."""
        manager = HookManager()
        for source, destination in ((self._handlers, manager._handlers), (self._observers, manager._observers)):
            for event, handlers in source.items():
                destination[event] = [
                    rebind(handler) if rebind else handler for handler in handlers
                    if not getattr(handler, 'agent_local', False)
                    and not getattr(getattr(handler, '__self__', None), 'agent_local', False)
                    and (exclude_owner is None or getattr(handler, "__self__", None) is not exclude_owner)
                ]
        return manager

    def notify(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Run every observer, then surface failures rather than silently losing facts.

        Observers that can safely degrade must handle and report their own errors.
        Process-level interrupts still propagate immediately.
        """
        errors = []
        for handler in tuple(self._observers[event]):
            try:
                handler(*args, **kwargs)
            except Exception as error:
                errors.append(error)
        if errors:
            for error in errors[1:]:
                errors[0].add_note(f"Observer {event} also failed: {type(error).__name__}: {error}")
            raise errors[0]

    def decide(self, event: str, *args: Any, **kwargs: Any) -> Any:
        """Run hooks in order; first non-None result blocks or alters flow."""

        for handler in self._handlers[event]:
            result = handler(*args, **kwargs)
            if result is not None:
                return result
        return None

    def trigger(self, event: str, *args: Any, **kwargs: Any) -> Any:
        """Compatibility entry point for existing control hooks; never notifies observers."""
        return self.decide(event, *args, **kwargs)

    def permission_snapshot(self) -> list[Any]:
        return [snapshot() for handler in self._handlers["PreToolUse"]
                if callable(snapshot := getattr(handler, "permission_snapshot", None))]
