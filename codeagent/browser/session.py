"""Browser ownership and handoff, independent of the execution backend."""
from __future__ import annotations

import threading
import time
from uuid import uuid4

from codeagent.runtime.cancellation import CancelledError
from codeagent.runtime.execution import ExecutionStopped
from codeagent.tools.base import ToolOutput


class BrowserSession:
    def __init__(self, conversation, backend):
        self.id = uuid4().hex
        self.conversation = conversation
        self.backend = backend
        self.lock = threading.RLock()
        self.control = 'agent'
        self.active = False
        self.owner = None
        self.generation = 0
        self.observed = -1
        self.started = False
        self.error = ''
        self.title = self.url = ''
        self.updated_at = None
        self.last_used = time.monotonic()

    def _valid(self):
        return self.backend.valid()

    def view(self):
        with self.lock:
            if self.control == 'closing' and self._closed_transport():
                self.control = 'closed'
            return dict(id=self.id, server=self.backend.name, control=self.control,
                started=self.started, busy=self.active, headless=self.backend.headless,
                title=self.title, url=self.url, updated_at=self.updated_at, error=self.error,
                idle_timeout_seconds=1800)

    def _closed_transport(self):
        return self.backend.is_closed()

    def _observe(self, result):
        page = getattr(result, 'browser_page', None)
        if page is not None:
            self.url = page['url'][:4096]
            self.title = page['title'][:500]
            self.updated_at = time.time()

    def call(self, name, arguments, owner, check, remaining):
        check()
        if not self._valid():
            self.close('浏览器配置或信任已变更，请重新开始。')
        with self.lock:
            if self.control != 'agent':
                return ToolOutput('浏览器当前由用户接管、正在切换或已关闭；尚未执行操作。交还后先获取 browser_snapshot。', status='blocked', retryable=False)
            if self.active or (self.owner is not None and self.owner is not owner):
                return ToolOutput('此浏览器正由另一个 Agent 操作，请等待其结束。', status='blocked', retryable=False)
            if self.generation and self.observed != self.generation and name != 'browser_snapshot':
                return ToolOutput('浏览器控制权或页面已变化。先调用 browser_snapshot 获取当前页面，再重新选择元素。', status='blocked', retryable=False)
            self.active, self.owner = True, owner
            self.last_used = time.monotonic()
        try:
            result = self.backend.call(name, arguments, check, remaining)
            self.started = self.backend.started
            if getattr(result, 'browser_closed', False) and result.status == 'success':
                self.backend.close()
            with self.lock:
                if self.control not in {'agent', 'pausing'}:
                    return result  # Concurrent close/revoke wins over a late response.
                self._observe(result)
                if result.outcome == 'unknown':
                    self.control = 'error'
                    self.error = '上次操作是否完成无法确认，请关闭浏览器后重新开始。'
                elif result.status == 'success':
                    self.error = ''
                    if name == 'browser_snapshot':
                        self.observed = self.generation
                    if name == 'browser_close':
                        self.title = self.url = ''
                        self.updated_at = time.time()
                        if getattr(result, 'browser_closed', False):
                            self.control = 'closing'
                else:
                    self.error = '浏览器操作失败，详情见本轮工具结果。'
            return result
        except (CancelledError, ExecutionStopped):
            self.close('执行已停止，已请求关闭浏览器。')
            raise
        except Exception:
            with self.lock:
                if self.control not in {'closing', 'closed'}:
                    self.control, self.error = 'error', '浏览器连接异常，请关闭后重新开始。'
            return ToolOutput(self.error, status='error', retryable=False)
        finally:
            with self.lock:
                self.active = False
                self.last_used = time.monotonic()
                if self.control == 'pausing':
                    self.control = 'human'

    def release(self, owner, check):
        with self.lock:
            if self.owner is not owner:
                return
            self.owner = None
            self.observed = -1
        try:
            check()
        except (CancelledError, ExecutionStopped):
            self.close('执行已停止，已请求关闭浏览器。')

    def takeover(self):
        if not self._valid():
            self.close('浏览器配置或信任已变更。')
        with self.lock:
            if self.backend.headless:
                raise ValueError('后台浏览器无法人工接管，请使用可见浏览器预设。')
            if self.control not in {'agent', 'pausing', 'human'} or not self.started:
                raise ValueError('浏览器尚未启动或状态异常，请先让 Agent 打开页面。')
            # No new calls can enter after this transition. The active call's
            # finally block grants control only after a known result returns.
            self.control = 'pausing' if self.active else 'human'
            self.last_used = time.monotonic()
        return self.view()

    def handback(self):
        if not self._valid():
            self.close('浏览器配置或信任已变更。')
        with self.lock:
            if self.control != 'human':
                raise ValueError('请等待浏览器接管完成后再交还。')
            self.control, self.active = 'resuming', True
        try:
            result = self.backend.call('browser_snapshot', {}, lambda: None, lambda: 15)
            with self.lock:
                if self.control != 'resuming':
                    return self.view()  # Concurrent close/revoke wins.
                if result.status != 'success':
                    self.control, self.error = 'error', '无法重新读取页面，请关闭浏览器后重新开始。'
                else:
                    self._observe(result)
                    self.generation += 1
                    self.observed = -1
                    self.control, self.error = 'agent', ''
        except Exception:
            with self.lock:
                if self.control == 'resuming':
                    self.control, self.error = 'error', '交还浏览器失败，请关闭后重新开始。'
        finally:
            with self.lock:
                self.active = False
                self.last_used = time.monotonic()
        return self.view()

    def close(self, reason='已请求关闭浏览器，临时页面和登录状态不会继续复用。'):
        with self.lock:
            self.control = 'closing'
            self.error = reason
            self.title = self.url = ''
        self.backend.close()
        return self.view()


class SessionManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.sessions = {}
        self.stopped = threading.Event()
        self.thread = None

    def get(self, key, create):
        with self.lock:
            if self.stopped.is_set():
                raise RuntimeError('Browser sessions are closed')
            session = self.sessions.get(key)
            if session is None or session.view()['control'] == 'closed':
                session = create()
                self.sessions[key] = session
            if self.thread is None:
                self.thread = threading.Thread(target=self._sweep, daemon=True, name='browser-idle-cleanup')
                self.thread.start()
            return session

    def list(self, conversation):
        with self.lock:
            sessions = [s for s in self.sessions.values() if s.conversation == conversation]
        for session in sessions:
            if not session._valid() and session.view()['control'] not in {'closed', 'closing'}:
                session.close('浏览器配置或信任已变更。')
        return [s.view() for s in sessions]

    def action(self, conversation, identifier, action):
        with self.lock:
            session = next((s for s in self.sessions.values() if s.conversation == conversation and s.id == identifier), None)
        if session is None:
            raise ValueError('浏览器会话已失效，请刷新状态。')
        return {'takeover': session.takeover, 'handback': session.handback, 'close': session.close}[action]()

    def close_conversation(self, conversation):
        with self.lock:
            sessions = [s for s in self.sessions.values() if s.conversation == conversation]
        for session in sessions:
            session.close()

    def expire(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            with session.lock:
                idle = not session.active and now - session.last_used >= 1800
                if idle and session.control not in {'closed', 'closing'}:
                    session.control = 'closing'
                else:
                    idle = False
            if idle:
                session.close('浏览器闲置 30 分钟，已请求关闭。')

    def _sweep(self):
        while not self.stopped.wait(30):
            self.expire()

    def close(self):
        self.stopped.set()
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.close()
        if self.thread:
            self.thread.join(1)
