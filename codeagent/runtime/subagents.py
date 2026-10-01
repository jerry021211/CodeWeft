"""Run-owned, single-level read-only subagents with bounded admission."""
from __future__ import annotations

import json
import math
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from codeagent.runtime.cancellation import CancellationToken, CancelledError
from codeagent.runtime.parallel import admission
from codeagent.runtime.tool_executor import submit
from codeagent.tools.base import ToolOutput

TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})


@dataclass
class SubagentRun:
    id: str
    description: str
    token: CancellationToken
    emitter: object
    background: bool
    parent_tool_use_id: str
    status: str = "queued"
    output: str = ""
    output_handle: str = ""
    delivered: bool = False
    started: float = field(default_factory=time.monotonic)
    future: Future | None = None
    interrupt: object = None
    finished: float | None = None

    def snapshot(self):
        return {
            "subagent_id": self.id, "description": self.description[:1000],
            "status": self.status, "background": self.background,
            "parent_tool_use_id": self.parent_tool_use_id,
            "output_handle": self.output_handle, "result": self.output[:4000],
            "delivered": self.delivered,
            "duration_ms": round(((self.finished or time.monotonic()) - self.started) * 1000),
        }


class SubagentRuntime:
    def __init__(self, config, emitter, cancellation, output_dir: Path, check, budget=None):
        self.config, self.emitter, self.cancellation = config, emitter, cancellation
        self.output_dir, self.check, self.budget = output_dir, check, budget
        self._lock = threading.RLock()
        self._runs: dict[str, SubagentRun] = {}
        self._executor = ThreadPoolExecutor(max_workers=config.max_subagents,
                                            thread_name_prefix="codeagent-child")
        self._closed = False

    def start(self, description, build, *, background=False, parent_tool_use_id=""):
        self.check()
        with self._lock:
            if self._closed:
                raise RuntimeError("Subagent runtime is closed")
            if sum(r.status not in TERMINAL for r in self._runs.values()) >= self.config.max_pending_subagents:
                raise ValueError("Subagent queue is full; collect existing results before spawning more")
            identifier = f"agent_{uuid4().hex}"
            token = CancellationToken(parent=self.cancellation)
            emitter = self.emitter.child(agent_id=identifier)
            # Build in the coordinator: factories must not race on parent state.
            child = build(emitter, token)
            record = SubagentRun(identifier, description, token, emitter, background, parent_tool_use_id)
            record.interrupt = child.execution_activity.interrupt_request if child.execution_activity else None
            self._runs[identifier] = record
            self._publish(record)
            try:
                record.future = submit(self._executor, self._execute, record, child)
            except BaseException:
                record.status = "failed"
                self._publish(record)
                raise
            return record

    def _publish(self, record):
        record.emitter.emit(f"subagent.{record.status}", record.snapshot())

    def _execute(self, record, child):
        try:
            with admission("subagents").enter(record.token.raise_if_cancelled):
                record.token.raise_if_cancelled()
                with self._lock:
                    record.status = "running"
                    self._publish(record)
                result = child.run(record.description)
                from codeagent.runtime.execution import is_execution_failure
                output = result.final_text or f"Subagent stopped: {result.stop_reason}"
                failed = is_execution_failure(result.stop_reason)
                if failed:
                    output = f"Error: Subagent failed ({result.stop_reason}): {output}"
                with self._lock:
                    record.output = output
                    record.status = "failed" if failed else "completed"
        except CancelledError as exc:
            with self._lock:
                record.status, record.output = "cancelled", f"Error: {exc}"
        except BaseException as exc:
            with self._lock:
                record.status, record.output = "failed", f"Error: {type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                record.finished = time.monotonic()
                try:
                    self.output_dir.mkdir(parents=True, exist_ok=True)
                    path = self.output_dir / f"{record.id}.txt"
                    path.write_text(record.output, encoding="utf-8")
                    record.output_handle = str(path)
                except OSError as exc:
                    record.status = "failed"
                    record.output += f"\nError saving subagent output: {exc}"
                self._publish(record)
        return ToolOutput(record.output, status="success" if record.status == "completed" else "error")

    def get(self, identifier):
        with self._lock:
            if identifier not in self._runs:
                raise ValueError("Unknown subagent in this run")
            return self._runs[identifier]

    def wait(self, record, *, check=True, timeout=None):
        from contextlib import nullcontext
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.budget.paused() if self.budget else nullcontext():
            while not record.future.done():
                if check:
                    self.check()
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                # Wait without occupying the tool executor or an SDK request slot.
                time.sleep(0.02)
            return record.future.result()

    def result(self, subagent_id, wait=False, timeout_seconds=30):
        if not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        record = self.get(subagent_id)
        if wait:
            self.wait(record, timeout=timeout_seconds)
        with self._lock:
            return json.dumps(record.snapshot(), ensure_ascii=False)

    def cancel(self, subagent_id):
        interrupt = None
        error = None
        with self._lock:
            record = self.get(subagent_id)
            if record.status not in TERMINAL:
                record.token.cancel("Subagent cancelled")
                record.status = "cancelling"
                interrupt = record.interrupt
                try:
                    self._publish(record)
                except BaseException as exc:
                    error = exc
        if interrupt:
            interrupt()
        if error is not None:
            raise error
        with self._lock:
            return json.dumps(record.snapshot(), ensure_ascii=False)

    def cancel_futures(self, futures):
        with self._lock:
            identifiers = [r.id for r in self._runs.values() if r.future in futures]
        for identifier in identifiers:
            self.cancel(identifier)

    def collect(self, *, wait=False):
        with self._lock:
            records = [r for r in self._runs.values() if r.background and not r.delivered]
        if wait:
            for record in records:
                self.wait(record)
        notifications = []
        with self._lock:
            for record in records:
                if record.future.done() and not record.delivered:
                    # Surface persistence failures rather than hiding a broken journal.
                    record.future.result()
                    notifications.append(record.snapshot())
        return notifications

    def delivered(self, identifiers):
        with self._lock:
            for identifier in identifiers:
                record = self.get(identifier)
                record.delivered = True
                record.emitter.emit("subagent.delivered", record.snapshot())

    def close(self):
        with self._lock:
            self._closed = True
            records = list(self._runs.values())
        errors = []
        for record in records:
            if record.status not in TERMINAL:
                try:
                    self.cancel(record.id)
                except BaseException as exc:
                    errors.append(exc)
        self._executor.shutdown(wait=True)
        # A durable sink failure must prevent a successful parent checkpoint.
        for record in records:
            if record.future:
                try:
                    record.future.result()
                except BaseException as exc:
                    errors.append(exc)
        if errors:
            raise errors[0]
