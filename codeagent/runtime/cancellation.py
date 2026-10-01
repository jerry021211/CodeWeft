"""Thread-safe cooperative cancellation primitives."""

from __future__ import annotations

import threading


class CancelledError(RuntimeError):
    """Raised when cooperative execution has been cancelled."""

    def __init__(self, reason: str = "Cancelled by user") -> None:
        self.reason = reason
        super().__init__(reason)


class ModelCallTimeout(CancelledError):
    """A runtime deadline, not a retryable provider/network error."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class CancellationToken:
    """A small thread-safe cancellation signal shared across runtime layers.

    Cancellation is cooperative: callers should invoke :meth:`raise_if_cancelled`
    at safe boundaries, or use :meth:`wait` instead of an uninterruptible sleep.
    The first cancellation reason wins so every observer reports the same cause.
    """

    def __init__(self, parent: CancellationToken | None = None) -> None:
        self._event = threading.Event()
        self._parent = parent
        self._lock = threading.Lock()
        self._reason = "Cancelled by user"
        self._reason_code: str | None = None

    @property
    def is_cancelled(self) -> bool:
        """Whether cancellation has been requested."""

        return self._event.is_set() or bool(self._parent and self._parent.is_cancelled)

    @property
    def reason(self) -> str:
        """The stable reason associated with this token."""
        if not self._event.is_set() and self._parent is not None and self._parent.is_cancelled:
            return self._parent.reason
        with self._lock:
            return self._reason

    def cancel(self, reason: str = "Cancelled by user", *, reason_code: str | None = None) -> None:
        """Request cancellation. Repeated calls do not replace the reason."""

        normalized_reason = str(reason).strip() or "Cancelled by user"
        with self._lock:
            if self._event.is_set():
                return
            if self._parent is not None and self._parent.is_cancelled:
                return
            self._reason = normalized_reason
            self._reason_code = reason_code
            self._event.set()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`CancelledError` when cancellation was requested."""

        if self._event.is_set():
            if self._reason_code in {"model_response_timeout", "model_call_timeout"}:
                raise ModelCallTimeout(self._reason_code)
            raise CancelledError(self.reason)
        if self._parent is not None:
            self._parent.raise_if_cancelled()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for cancellation and return ``True`` if it was requested."""

        if self._parent is None:
            return self._event.wait(timeout)
        import time
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_cancelled:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            self._event.wait(0.05 if remaining is None else min(0.05, remaining))
        return self.is_cancelled
