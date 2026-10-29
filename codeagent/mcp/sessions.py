"""Process-local ownership for opt-in, stateful stdio services.

Leases survive router replacement so a configuration reload cannot hand an
in-use service to another execution before its old transport has closed.
"""

import os
import threading
from pathlib import Path
from weakref import WeakValueDictionary


class ExecutionLease:
    def __init__(self):
        self.lock = threading.RLock()
        self.owner = None
        self.router = None
        self.cleaning = False
        self.uncertain = False

    def claim(self, owner, router):
        with self.lock:
            if owner is None:
                return "任务独占服务需要执行上下文，请通过 Agent 工具调用。"
            if self.cleaning:
                return "服务正在清理上一次任务，请稍后重试。"
            if self.owner is not None and (self.owner is not owner or self.router is not router):
                return "服务正被其他任务占用，请等待该任务结束。"
            if self.uncertain:
                return "上次操作结果未知，本次执行不能继续使用此服务；任务结束后将清理连接。"
            self.owner, self.router = owner, router
            with _lock:
                _active_leases[id(self)] = self
            return None

    def invalidate(self, owner, router):
        with self.lock:
            if self.owner is owner and self.router is router:
                self.uncertain = True

    def begin_cleanup(self, owner, router):
        with self.lock:
            if self.owner is not owner or self.router is not router or self.cleaning:
                return False
            self.cleaning = True
            return True

    def released(self, router):
        with self.lock:
            if self.router is router:
                self.owner = self.router = None
                self.cleaning = self.uncertain = False
                with _lock:
                    _active_leases.pop(id(self), None)


_leases = WeakValueDictionary()
# Keep uncertain transports quarantined even if their retired router is collected.
_active_leases = {}
_lock = threading.Lock()


def execution_lease(workspace: Path, server: str) -> ExecutionLease:
    key = (os.path.normcase(str(workspace.resolve())), server)
    with _lock:
        lease = _leases.get(key)
        if lease is None:
            lease = ExecutionLease()
            _leases[key] = lease
        return lease
