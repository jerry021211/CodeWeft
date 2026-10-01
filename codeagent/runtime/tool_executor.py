"""Partition ordered tool calls at exclusive barriers."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context


def batches(calls, parallel_kind, width):
    pending = []
    kind = None
    for index, call in enumerate(calls):
        current = parallel_kind(call)
        if pending and (not current or current != kind or len(pending) >= width):
            yield kind, pending
            pending = []
        if not current:
            yield None, [index]
        else:
            kind = current
            pending.append(index)
    if pending:
        yield kind, pending


def submit(executor: ThreadPoolExecutor, function, *args):
    # A fresh context per submission preserves tracing without sharing Context.
    return executor.submit(copy_context().run, function, *args)
