"""Bounded, process-wide I/O admission; independent from Team workers."""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParallelConfig:
    enabled: bool = True
    max_tools: int = 4
    max_subagents: int = 3
    max_pending_subagents: int = 16

    def __post_init__(self):
        for name in ("max_tools", "max_subagents", "max_pending_subagents"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")


class Admission:
    def __init__(self, capacity: int):
        self._slots = threading.BoundedSemaphore(capacity)

    @contextmanager
    def enter(self, check=lambda: None):
        while True:
            check()
            if self._slots.acquire(timeout=0.05):
                break
        try:
            check()
            yield
        finally:
            self._slots.release()


_lock = threading.Lock()
_limits: dict[str, Admission] = {}


def admission(kind: str) -> Admission:
    """Process capacities are read once, on first use, after dotenv loading."""
    defaults = {"models": 8, "subagents": 8, "tools": 16}
    with _lock:
        if kind not in _limits:
            value = int(os.getenv(f"CODEAGENT_MAX_CONCURRENT_{kind.upper()}", defaults[kind]))
            if value < 1:
                raise ValueError(f"Concurrent {kind} limit must be positive")
            _limits[kind] = Admission(value)
        return _limits[kind]
