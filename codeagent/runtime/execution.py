"""One logical execution's budgets, independent of context projection."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from threading import RLock, get_ident
from typing import Any, Callable, Iterator

MAX_EXECUTION_ROUNDS = 200
WRAP_UP_ROUND = 185
RETIRED_CALL_LIMIT_REASONS = frozenset({"budget_exceeded:model_calls", "budget_exceeded:tool_calls"})


@dataclass(slots=True)
class RoundBudget:
    """One logical execution, retained across yields and checkpoints."""
    scope_id: str = ''
    rounds: int = 0

    def begin(self, scope_id: str) -> None:
        if scope_id != self.scope_id:
            self.scope_id, self.rounds = scope_id, 0

    def admit(self) -> None:
        if self.rounds >= MAX_EXECUTION_ROUNDS:
            raise ExecutionStopped(f'max_iterations:{MAX_EXECUTION_ROUNDS}')
        self.rounds += 1

    def reminder(self) -> str:
        if self.rounds < WRAP_UP_ROUND:
            return ''
        return (f'本次执行已进入第 {WRAP_UP_ROUND} 轮开始的收尾阶段，总上限为 {MAX_EXECUTION_ROUNDS} 轮。'
                '请停止扩展任务范围，优先完成必要验证、整理已完成修改、明确未完成项和阻碍，'
                '并在剩余轮数内向用户提交最终答复。不要为了收尾虚报完成或跳过必要验证。')


class ExecutionStopped(BaseException):
    """Control flow: ordinary tool/provider error recovery must not retry it."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def is_execution_failure(reason: str) -> bool:
    return reason.startswith(("recovery_failed", "max_iterations", "runtime_contract",
                              "loop_detected:", "budget_exceeded:"))


@dataclass(slots=True)
class BudgetState:
    model_calls: int = 0
    tool_calls: int = 0
    total_tokens: int = 0
    unknown_usage_calls: int = 0
    active_seconds: float = 0.0
    stop_reason: str = ""


class RunBudget:
    """Atomic reservations; parent/child active intervals are counted once."""

    def __init__(self, config: Any, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.clock = clock
        self.state = BudgetState()
        self._lock = RLock()
        self._running = 0
        self._paused = 0
        self._branches: dict[int, list[int]] = {}
        self._last_tick = clock()

    def _tick(self) -> None:
        now = self.clock()
        if any(running and not paused for running, paused in self._branches.values()):
            self.state.active_seconds += max(0.0, now - self._last_tick)
        self._last_tick = now

    def reset(self) -> None:
        with self._lock:
            self.state = BudgetState()
            self._last_tick = self.clock()

    @contextmanager
    def running(self) -> Iterator[None]:
        with self._lock:
            self._tick()
            self._running += 1
            branch = self._branches.setdefault(get_ident(), [0, 0])
            branch[0] += 1
        try:
            yield
        finally:
            with self._lock:
                self._tick()
                self._running -= 1
                branch[0] -= 1
                if branch == [0, 0]:
                    self._branches.pop(get_ident(), None)

    @contextmanager
    def paused(self) -> Iterator[None]:
        with self._lock:
            self._tick()
            self._paused += 1
            branch = self._branches.setdefault(get_ident(), [0, 0])
            branch[1] += 1
        try:
            yield
        finally:
            with self._lock:
                self._tick()
                self._paused -= 1
                branch[1] -= 1
                if branch == [0, 0]:
                    self._branches.pop(get_ident(), None)

    def check(self) -> None:
        with self._lock:
            self._tick()
            if self.state.active_seconds >= self.config.max_active_seconds:
                self.state.stop_reason = self.state.stop_reason or "budget_exceeded:active_time"
            if self.config.max_total_tokens and self.state.total_tokens >= self.config.max_total_tokens:
                self.state.stop_reason = self.state.stop_reason or "budget_exceeded:tokens"
            if self.state.stop_reason:
                raise ExecutionStopped(self.state.stop_reason)

    def remaining_seconds(self) -> float:
        with self._lock:
            self.check()
            return self.config.max_active_seconds - self.state.active_seconds

    def reserve(self, kind: str) -> None:
        with self._lock:
            self.check()
            name = f"{kind}_calls"
            # Calls remain observable, but are no longer an admission limit.
            setattr(self.state, name, getattr(self.state, name) + 1)

    def invoke(self, client: Any, **kwargs: Any) -> Any:
        self.reserve("model")
        response = None
        try:
            response = client.create_message(**kwargs)
        finally:
            # Exactly once per actual client invocation, never from replayable events.
            usage = getattr(response, "usage", None)
            with self._lock:
                if usage is None or not usage.available or usage.estimated:
                    self.state.unknown_usage_calls += 1
                else:
                    self.state.total_tokens += usage.total_tokens
        self.check()
        return response

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._tick()
            return asdict(self.state)

    def restore(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.state = BudgetState(**payload)
            if self.state.stop_reason in RETIRED_CALL_LIMIT_REASONS:
                self.state.stop_reason = ""
            self._last_tick = self.clock()


class BudgetedClient:
    """Charge side calls and their forks to the same root execution."""

    def __init__(self, client: Any, budget: RunBudget) -> None:
        self.client = client
        self.budget = budget

    def create_message(self, **kwargs: Any) -> Any:
        return self.budget.invoke(self.client, **kwargs)

    def fork(self, **kwargs: Any) -> BudgetedClient:
        fork = getattr(self.client, "fork", None)
        return BudgetedClient(fork(**kwargs) if callable(fork) else self.client, self.budget)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)
