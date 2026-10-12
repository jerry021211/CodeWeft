"""Isolated MCP connections with bounded, cancellable synchronous tool calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Callable

import httpx2
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from codeagent.mcp.config import McpServerConfig, load_mcp_servers
from codeagent.mcp.trust import McpTrustStore, resolved_server, server_fingerprint
from codeagent.runtime.cancellation import CancelledError
from codeagent.runtime.data_paths import RuntimeDataPaths
from codeagent.tools import ToolDefinition, ToolRegistry
from codeagent.tools.base import ToolOutput


def _unknown_result(message: str) -> ToolOutput:
    return ToolOutput(message + "; remote completion is unknown. Do not automatically retry.",
                      status="error", outcome="unknown", retryable=False, retry_safe=False)


class McpTool:
    """One execution-local adapter; connections may be shared, budgets may not."""

    def __init__(self, router: "McpRouter", server: str, remote_tool: Any) -> None:
        self.router, self.server, self.remote_tool = router, server, remote_tool
        self.remote_name = remote_tool.name
        self._check: Callable[[], None] = lambda: None
        self._remaining: Callable[[], float] = lambda: float('inf')
        self.definition = ToolDefinition(
            name=_public_tool_name(server, remote_tool.name),
            description=remote_tool.description or f"MCP tool from {server}",
            input_schema=remote_tool.input_schema,
        )

    def isolated_copy(self) -> "McpTool":
        return McpTool(self.router, self.server, self.remote_tool)

    def bind_runtime(self, *, cancellation_check, remaining_seconds) -> None:
        self._check, self._remaining = cancellation_check, remaining_seconds

    def run(self, **kwargs: Any) -> ToolOutput:
        result = self.router.call_tool(self.server, self.remote_name, kwargs,
                                      cancellation_check=self._check, remaining_seconds=self._remaining)
        known = {('tavily', 'tavily-search'): 'web_search',
                 ('brave-search', 'brave_web_search'): 'web_search', ('fetch', 'fetch'): 'webpage'}
        result.output_policy = known.get((self.server, self.remote_name), 'default')
        if result.output_policy == 'webpage':
            result.source_url = kwargs.get('url')
        return result


class McpRouter:
    """One owning async task/exit stack per server, with fail-closed admission."""

    def __init__(self, config_path: str | Path = "mcp.json", *,
                 workspace_root: str | Path | None = None, force_workspace_cwd: bool = False,
                 trust_checker: Callable[[McpServerConfig], bool] | None = None) -> None:
        if force_workspace_cwd and workspace_root is None:
            raise ValueError("force_workspace_cwd requires workspace_root")
        self.workspace_root = Path(workspace_root or Path(config_path).resolve().parent).resolve()
        self.force_workspace_cwd = bool(force_workspace_cwd)
        self.servers = [resolved_server(item) for item in load_mcp_servers(config_path)]
        store = McpTrustStore(RuntimeDataPaths.default().root / 'state' / 'mcp-trust.db')
        self._trust_checker = trust_checker or (lambda server: store.is_trusted(self.workspace_root, server))
        self._clients: dict[str, Client] = {}
        self._tools: list[McpTool] = []
        self._states: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._connect_locks: dict[str, asyncio.Lock] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._lifecycle_lock = threading.RLock()
        self._closed = False
        self._retired = False
        self._active_calls = 0
        for server in self.servers:
            self._state(server, 'not_connected')

    def _state(self, server, status, *, error=None, tool_count=0):
        with self._lifecycle_lock:
            self._states[server.name] = dict(name=server.name, status=status, error=error,
                                            tool_count=tool_count, fingerprint=server_fingerprint(server))

    def states(self) -> dict[str, dict[str, Any]]:
        """A non-starting snapshot, safe for configuration/status views."""
        with self._lifecycle_lock:
            return {key: dict(value) for key, value in self._states.items()}

    def start(self, *, cancellation_check: Callable[[], None] = lambda: None) -> None:
        cancellation_check()
        with self._lifecycle_lock:
            if self._retired:
                raise RuntimeError("MCP configuration changed; start a new turn to refresh tools")
            if self._closed:
                raise RuntimeError("MCP router is closed")
            if not self.servers:
                return
            if self._thread is None:
                self._thread = threading.Thread(target=self._thread_main, name="codeagent-mcp", daemon=True)
                self._thread.start()
        deadline = time.monotonic() + max(getattr(s, 'startup_timeout', 15) for s in self.servers) + 5
        while not self._ready.wait(0.05):
            cancellation_check()
            if self._closed or self._retired:
                raise RuntimeError("MCP router is closed")
            if time.monotonic() >= deadline:
                raise RuntimeError("MCP startup deadline exceeded")
        cancellation_check()
        if self._retired or self._closed:
            raise RuntimeError("MCP router is closed")
        if self._startup_error is not None:
            raise RuntimeError(f"MCP startup failed: {self._startup_error}") from self._startup_error

    def register_tools(self, registry: ToolRegistry, *, cancellation_check=lambda: None) -> None:
        self.start(cancellation_check=cancellation_check)
        # Stable schemas and separate runtime bindings for every Agent/run.
        for tool in sorted(self._tools, key=lambda item: item.definition.name):
            registry.register(tool.isolated_copy())

    def list_tools(self) -> list[str]:
        self.start()
        return sorted(tool.definition.name for tool in self._tools)

    def connect_server(self, name: str) -> None:
        self.start()
        server = next((item for item in self.servers if item.name == name), None)
        if server is None:
            raise ValueError('Unknown MCP server')
        with self._lifecycle_lock:
            if self._closed or self._retired:
                raise ValueError('MCP configuration changed; refresh before connecting')
            future = asyncio.run_coroutine_threadsafe(self._reconnect(server), self._loop)
        try:
            future.result(timeout=server.startup_timeout + 5)
        except FutureTimeout:
            future.cancel()
            raise ValueError('MCP connection deadline exceeded') from None

    async def _reconnect(self, server):
        async with self._connect_locks.setdefault(server.name, asyncio.Lock()):
            if self._closed or self._retired or server.name in self._clients:
                return
            previous = self._tasks.get(server.name)
            if previous is not None:
                previous.cancel()
                await asyncio.gather(previous, return_exceptions=True)
            ready = asyncio.Event()
            self._tasks[server.name] = asyncio.create_task(self._serve(server, ready))
            await ready.wait()

    def call_tool(self, server: str, name: str, arguments: dict[str, Any], *,
                  cancellation_check=lambda: None, remaining_seconds=lambda: float('inf')) -> ToolOutput:
        cancellation_check()
        config = next(item for item in self.servers if item.name == server)
        if not self._trust_checker(config):
            return ToolOutput("MCP server requires trust approval before use", status='blocked')
        budget = min(config.call_timeout, remaining_seconds())
        if not math.isfinite(budget) or budget <= 0:
            return ToolOutput("MCP time budget exhausted before dispatch", status='blocked')
        # Admission and retirement are atomic: every accepted call belongs to the
        # draining generation, and no stale adapter can add work after retirement.
        with self._lifecycle_lock:
            if self._retired:
                return ToolOutput("MCP configuration changed; start a new turn to refresh tools", status='blocked')
            if self._closed or self._loop is None or not self._loop.is_running() or server not in self._clients:
                return ToolOutput("MCP server is disconnected", status='error')
            invocation = self._invoke(server, name, arguments, budget)
            try:
                future = asyncio.run_coroutine_threadsafe(invocation, self._loop)
            except RuntimeError:
                invocation.close()
                return ToolOutput("MCP server is disconnected", status='error')
            self._active_calls += 1
        deadline = time.monotonic() + budget
        try:
            while True:
                cancellation_check()
                remaining = min(deadline - time.monotonic(), remaining_seconds())
                if remaining <= 0:
                    raise TimeoutError('MCP call timed out')
                try:
                    result = future.result(timeout=min(0.05, remaining))
                    return _tool_result_text(result)
                except FutureTimeout:
                    if future.done():
                        raise
        except CancelledError as exc:
            future.cancel()  # Cancels the SDK request task, without closing other calls/connections.
            exc.tool_output = _unknown_result('MCP call cancelled')
            raise
        except TimeoutError:
            future.cancel()
            return _unknown_result('MCP call timed out')
        except Exception:
            future.cancel()
            return _unknown_result('MCP connection or request failed')
        finally:
            with self._lifecycle_lock:
                self._active_calls -= 1
                if self._retired and not self._active_calls:
                    self._request_stop()

    async def _invoke(self, server, name, arguments, timeout):
        async with asyncio.timeout(timeout):
            return await self._clients[server].call_tool(name, arguments, read_timeout_seconds=timeout)

    def retire(self) -> None:
        """Reject new calls, then release transports after admitted calls finish.

        Non-blocking so configuration edits never wait for a remote request.
        Existing call budgets/cancellation still bound the drain period.
        """
        with self._lifecycle_lock:
            self._retired = True
            if not self._active_calls:
                self._request_stop()

    def _request_stop(self) -> None:
        # Caller owns _lifecycle_lock; joining must happen outside it.
        self._closed = True
        if self._loop is not None and self._stop_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass  # The loop has already exited.

    def close(self) -> None:
        with self._lifecycle_lock:
            self._request_stop()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=6)

    def __enter__(self) -> "McpRouter":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:
            self._startup_error = exc
        finally:
            self._ready.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        if self._closed:
            self._stop_event.set()
        events = [asyncio.Event() for _ in self.servers]
        self._tasks = {server.name: asyncio.create_task(self._serve(server, ready))
                       for server, ready in zip(self.servers, events)}
        readiness = asyncio.create_task(self._wait_ready(events))
        stopped = asyncio.create_task(self._stop_event.wait())
        try:
            await asyncio.wait([readiness, stopped], return_when=asyncio.FIRST_COMPLETED)
            self._ready.set()
            if not self._closed:
                await stopped
        finally:
            self._stop_event.set()
            tasks = list(self._tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            readiness.cancel()
            stopped.cancel()
            await asyncio.gather(readiness, stopped, return_exceptions=True)

    async def _wait_ready(self, events):
        await asyncio.gather(*(event.wait() for event in events))

    async def _serve(self, server: McpServerConfig, ready: asyncio.Event) -> None:
        # Entry and exit MUST stay in this same task: SDK transports own AnyIO cancel scopes.
        timer = None
        discovered = False
        try:
            if server.config_error:
                self._state(server, 'error', error=server.config_error)
                return
            if not self._trust_checker(server):
                self._state(server, 'awaiting_trust')
                return
            if self._closed or self._retired:
                return
            self._state(server, 'connecting')
            task = asyncio.current_task()
            timer = self._loop.call_later(server.startup_timeout, task.cancel)
            async with AsyncExitStack() as stack:
                client = await stack.enter_async_context(
                    Client(await self._transport(server, stack), read_timeout_seconds=server.startup_timeout))
                tools, cursor, seen = [], None, set()
                while True:
                    listing = await client.list_tools(cursor=cursor)
                    tools.extend(McpTool(self, server.name, tool) for tool in listing.tools)
                    cursor = listing.next_cursor
                    if cursor is None:
                        break
                    if cursor in seen:
                        raise ValueError('MCP server repeated a pagination cursor')
                    seen.add(cursor)
                names = {tool.definition.name for tool in tools}
                if len(names) != len(tools) or names.intersection(tool.definition.name for tool in self._tools):
                    raise ValueError('Duplicate MCP tool names')
                timer.cancel()
                self._clients[server.name] = client
                self._tools.extend(tools)
                discovered = True
                self._state(server, 'connected', tool_count=len(tools))
                ready.set()
                await self._stop_event.wait()
        except asyncio.CancelledError:
            self._state(server, 'disconnected' if self._closed else 'error',
                        error=None if self._closed else '连接或工具发现超时，请检查服务或调大启动时限。')
        except Exception as exc:
            # Error strings may contain tokens/headers; expose only the class and an actionable hint.
            self._state(server, 'error', error=f'连接或工具发现失败（{type(exc).__name__}），请检查命令、地址和凭据。')
        finally:
            if timer is not None:
                timer.cancel()
            self._clients.pop(server.name, None)
            if discovered:
                self._tools = [tool for tool in self._tools if tool.server != server.name]
            ready.set()

    async def _transport(self, server: McpServerConfig, stack: AsyncExitStack):
        if server.transport == "stdio":
            cwd = self.workspace_root if self.force_workspace_cwd else (server.cwd or self.workspace_root)
            return stdio_client(StdioServerParameters(command=server.command, args=list(server.args),
                                                       env=server.env, cwd=str(cwd)))
        if server.transport in {"http", "streamable-http"}:
            if not server.headers:
                return server.url
            http_client = await stack.enter_async_context(httpx2.AsyncClient(headers=server.headers))
            return streamable_http_client(server.url, http_client=http_client)
        raise ValueError(f"Unsupported MCP transport: {server.transport}")


def _public_tool_name(server: str, tool: str) -> str:
    original = f"mcp__{server}__{tool}"
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", original)
    if safe != original or len(safe) > 64 or '__' in server or '__' in tool:
        identity = json.dumps([server, tool], ensure_ascii=False)
        return safe[:51] + '_' + hashlib.sha256(identity.encode()).hexdigest()[:12]
    return safe


def _tool_result_text(result: Any) -> ToolOutput:
    parts: list[str] = []
    media = []
    for item in result.content:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
        else:
            kind = getattr(item, 'type', 'unknown')
            uri = getattr(item, 'uri', None) or getattr(getattr(item, 'resource', None), 'uri', None)
            if isinstance(uri, str) and uri.startswith('data:'):
                uri = None
            # The current provider bridge is text-only. Never serialize inline
            # binary data into text or claim the model has seen the media.
            media.append({'type': kind, 'uri': uri, 'supported': False})
            parts.append(f'[unsupported MCP {kind}; resource={uri or "unavailable"}]')
    structured_json = result.structured_content is not None
    if structured_json:
        payload = ({'structured_content': result.structured_content, 'text_content': parts}
                   if parts else result.structured_content)
        text = json.dumps(payload, ensure_ascii=False)
    else:
        text = '\n'.join(parts)
    output = ToolOutput(text, status='error' if getattr(result, 'is_error', False) else 'success')
    output.media_references = media
    output.structured_json = structured_json
    return output
