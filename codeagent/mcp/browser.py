"""Compatibility adapter for conversation-owned Playwright MCP sessions."""
from __future__ import annotations

import re
from uuid import uuid4

from codeagent.browser.session import BrowserSession as SharedBrowserSession, SessionManager
from codeagent.mcp.approval import is_bundled_browser
from codeagent.mcp.router import McpRouter, McpTool


class BrowserTool(McpTool):
    def __init__(self, original, session):
        super().__init__(original.router, original.server, original.remote_tool, original._owner)
        self.session = session

    def isolated_copy(self, owner=None):
        copied = BrowserTool(self, self.session)
        copied._owner = owner if owner is not None else object()
        return copied

    def run(self, **kwargs):
        return self.session.call(self.remote_name, kwargs, self._owner, self._check, self._remaining)

    def close(self):
        self.session.release(self._owner, self._check)


class McpBrowserBackend:
    def __init__(self, parent, server, router_factory=McpRouter):
        self.parent, self.server = parent, server
        self.name = server.name
        self.headless = '--headless' in server.args
        self.started = False
        self.router = router_factory(workspace_root=parent.workspace_root,
            force_workspace_cwd=parent.force_workspace_cwd, servers=[server],
            session_workspace=parent.workspace_root / '.browser-sessions' / uuid4().hex,
            trust_checker=parent._trust_checker)

    def valid(self):
        return not self.parent._retired and not self.parent._closed and self.parent._trust_checker(self.server)

    def is_closed(self):
        thread = self.router._thread
        return (thread is None or not thread.is_alive()) and (
            self.server.name not in self.router._transport_closed
            or self.router._transport_closed[self.server.name])

    def call(self, name, arguments, check, remaining):
        self.router.start(cancellation_check=check)
        self.started = True
        result = self.router.call_tool(self.server.name, name, arguments,
            owner=self, cancellation_check=check, remaining_seconds=remaining)
        title = re.search(r'^- Page [Tt]itle: (.*)$', str(result), re.MULTILINE)
        url = re.search(r'^- Page URL: (.*)$', str(result), re.MULTILINE)
        if url:
            result.browser_page = dict(url=url[1].strip(), title=title[1].strip() if title else '')
        return result

    def close(self):
        self.router.close()


class BrowserSession(SharedBrowserSession):
    def __init__(self, conversation, parent, server, *, router_factory=McpRouter):
        super().__init__(conversation, McpBrowserBackend(parent, server, router_factory))
        self.parent, self.server, self.router = parent, server, self.backend.router


class BrowserSessions(SessionManager):
    def adapt(self, tool, conversation):
        server = next(item for item in tool.router.servers if item.name == tool.server)
        if not conversation or server.lifecycle != 'execution' or not is_bundled_browser(server):
            return tool
        key = (conversation, str(tool.router.workspace_root), server.name)
        session = self.get(key, lambda: BrowserSession(conversation, tool.router, server))
        return BrowserTool(tool, session)

    def retire(self, parent):
        with self.lock:
            sessions = [s for s in self.sessions.values() if s.parent is parent]
        for session in sessions:
            session.close('浏览器配置或信任已变更。')
