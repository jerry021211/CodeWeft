"""Direct Playwright runtime. All Playwright objects stay on one owning loop."""
from __future__ import annotations

import asyncio
import json
import math
import sys
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from uuid import uuid4

from codeagent.runtime.cancellation import CancelledError
from codeagent.runtime.execution import ExecutionStopped
from codeagent.tools.base import ToolOutput


class PlaywrightBackend:
    name = '内置浏览器 · Playwright'

    def __init__(self, *, channel='chrome', headless=False):
        self.channel, self.headless = channel, headless
        self.started = False
        self._lock = threading.RLock()
        self._loop = self._thread = self._cleanup = None
        self._closing = False
        self._closed = threading.Event()
        self._pw = self._browser = self._context = self._page = None
        self._calls = set()
        self._pages = {}
        self._frames = {}
        self._snapshot_id = ''

    def valid(self):
        return not self._closing

    def is_closed(self):
        return self._closed.is_set()

    def _start_loop(self):
        # Windows web servers may use a selector loop, which cannot spawn the
        # Playwright driver. This independent loop also avoids cross-thread API use.
        self._loop = asyncio.ProactorEventLoop() if sys.platform == 'win32' else asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name='builtin-browser')
        self._thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def call(self, name, arguments, check, remaining):
        check()
        seconds = min(60., remaining())
        if not math.isfinite(seconds) or seconds <= 0:
            return ToolOutput('Browser time budget exhausted; no operation started.', status='blocked')
        with self._lock:
            if self._closing:
                return ToolOutput('Browser is closing; no operation started.', status='blocked')
            if self._loop is None:
                self._start_loop()
            future = asyncio.run_coroutine_threadsafe(self._execute(name, arguments), self._loop)
        deadline = time.monotonic() + seconds
        try:
            while True:
                check()
                available = deadline - time.monotonic()
                if available <= 0:
                    future.cancel()
                    return ToolOutput('Browser operation timed out; completion is unknown. Close the browser before continuing.', status='error', outcome='unknown')
                try:
                    return future.result(timeout=min(.1, available))
                except FutureTimeout:
                    if future.done():
                        raise
        except (CancelledError, ExecutionStopped):
            future.cancel()
            self.close()
            raise

    async def _ensure_browser(self):
        if self._browser is not None:
            if not self._browser.is_connected():
                raise RuntimeError('Browser disconnected')
            return
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            channel=None if self.channel == 'chromium' else self.channel, headless=self.headless)
        self._context = await self._browser.new_context(accept_downloads=False)
        self._context.set_default_timeout(10000)
        self._context.on('page', self._track_page)
        self._page = await self._context.new_page()
        self.started = True

    def _invalidate(self, *args):
        self._snapshot_id = ''
        self._frames = {}

    def _track_page(self, page):
        identifier = uuid4().hex
        self._pages[identifier] = page
        self._page = page
        self._invalidate()
        page.on('framenavigated', self._invalidate)
        page.on('close', self._invalidate)

    def _current_page(self):
        if self._page is None or self._page.is_closed():
            self._page = next((p for p in self._pages.values() if not p.is_closed()), None)
        if self._page is None:
            raise ValueError('No open page. Use browser_navigate to open one.')
        return self._page

    async def _snapshot(self):
        page = self._current_page()
        token = uuid4().hex
        self._snapshot_id = token
        self._frames = {}
        frames, remaining = [], 32000
        for frame in page.frames[:16]:
            identifier = uuid4().hex
            text = await frame.locator('body').aria_snapshot(timeout=5000)
            if self._snapshot_id != token:
                raise ValueError('Page navigated during observation; read browser_snapshot again.')
            self._frames[identifier] = frame
            frames.append(dict(frame_id=identifier, url=frame.url, aria=text[:remaining], truncated=len(text) > remaining))
            remaining -= min(len(text), remaining)
            if remaining <= 0:
                break
        return dict(snapshot_id=token, frames=frames, omitted_frames=max(0, len(page.frames) - len(frames)))

    async def _locator(self, args):
        if not self._snapshot_id or args['snapshot_id'] != self._snapshot_id:
            raise ValueError('Observation is stale. Read browser_snapshot again before interacting.')
        frame = self._frames.get(args['frame_id'])
        if frame is None or frame.is_detached():
            raise ValueError('Frame is stale. Read browser_snapshot again.')
        target = args['target']
        if set(target) == {'role', 'name'} and all(isinstance(v, str) and v for v in target.values()):
            locator = frame.get_by_role(target['role'], name=target['name'], exact=True)
        elif len(target) == 1 and next(iter(target)) in {'label', 'text', 'test_id'}:
            key, value = next(iter(target.items()))
            if not isinstance(value, str) or not value:
                raise ValueError('Target must be a nonempty string.')
            locator = (frame.get_by_test_id(value) if key == 'test_id' else
                       frame.get_by_label(value, exact=True) if key == 'label' else
                       frame.get_by_text(value, exact=True))
        else:
            raise ValueError('Use exactly role+name, label, text, or test_id for target.')
        if await locator.count() != 1:
            raise ValueError('Target is missing or ambiguous; read a fresh snapshot and choose one element.')
        # A navigation may have happened while resolving the target.
        if args['snapshot_id'] != self._snapshot_id:
            raise ValueError('Page changed while resolving the target; read browser_snapshot again.')
        return locator

    async def _dispatch(self, name, args):
        from codeagent.browser.tools import web_url
        if name == 'browser_navigate':
            web_url(args['url'])
            if args.get('new_tab') or self._page is None or self._page.is_closed():
                self._page = await self._context.new_page()
            self._invalidate()
            await self._page.goto(args['url'], wait_until='domcontentloaded', timeout=15000)
        elif name == 'browser_snapshot':
            return await self._snapshot()
        elif name in {'browser_click', 'browser_fill', 'browser_press_key'}:
            locator = await self._locator(args)
            self._invalidate()
            if name == 'browser_click':
                await locator.click(timeout=10000)
            elif name == 'browser_fill':
                await locator.fill(args['value'], timeout=10000)
            else:
                await locator.press(args['key'], timeout=10000)
        elif name == 'browser_wait_for':
            if set(args) == {'seconds'} and type(args['seconds']) in {int, float} and 0 <= args['seconds'] <= 30:
                await asyncio.sleep(args['seconds'])
            elif set(args) == {'text'} and isinstance(args['text'], str) and args['text']:
                await self._current_page().get_by_text(args['text'], exact=False).first.wait_for(timeout=15000)
            else:
                raise ValueError('Supply either nonempty text or seconds between 0 and 30.')
        elif name == 'browser_tabs':
            action = args['action']
            if action not in {'list', 'select', 'close'}:
                raise ValueError('Unknown tab action.')
            if action != 'list':
                page = self._pages.get(args.get('tab_id'))
                if page is None or page.is_closed():
                    raise ValueError('Unknown tab_id; list tabs first.')
                self._invalidate()
                if action == 'select':
                    self._page = page
                    await page.bring_to_front()
                else:
                    await page.close()
            return dict(tabs=[dict(tab_id=i, url=p.url, title=await p.title(), active=p is self._page)
                              for i, p in self._pages.items() if not p.is_closed()])
        elif name != 'browser_close':
            raise ValueError('Unknown browser operation.')
        return dict(completed=True)

    async def _execute(self, name, args):
        task = asyncio.current_task()
        self._calls.add(task)
        launching = self._browser is None
        try:
            if self._closing:
                return ToolOutput('Browser is closing.', status='blocked')
            # Closing an unused session must not launch a browser.
            if name == 'browser_close':
                result = ToolOutput('Browser close requested. A new conversation turn can open a fresh browser.')
                result.browser_closed = True
                return result
            await self._ensure_browser()
            payload = await self._dispatch(name, args)
            page = next((p for p in [self._page] if p and not p.is_closed()), None)
            info = dict(url=page.url, title=await page.title()) if page else dict(url='', title='')
            result = ToolOutput(json.dumps({**info, **payload}, ensure_ascii=False))
            result.browser_page = info
            return result
        except ImportError:
            return ToolOutput('内置浏览器依赖未安装。请安装项目的 browser 扩展：python -m pip install ".[browser]"。', status='error')
        except ValueError as exc:
            return ToolOutput(str(exc), status='error', deterministic=True)
        except Exception as exc:
            # Browser errors can include credentials or typed values. Only expose
            # the class; uncertain interactions must never be automatically replayed.
            hint = ('无法启动内置浏览器，请检查 Playwright 依赖和已安装的 Chrome/Edge；使用 Chromium 时先运行 python -m playwright install chromium。'
                    if launching else '浏览器操作未完成或结果无法确认，请关闭浏览器后重新开始。')
            return ToolOutput(f'{hint} ({type(exc).__name__})', status='error', outcome='unknown')
        finally:
            self._calls.discard(task)

    async def _dispose(self):
        try:
            tasks = list(self._calls)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if self._browser:
                    await self._browser.close()
            finally:
                if self._pw:
                    await self._pw.stop()
            self._closed.set()
        finally:
            if self._closed.is_set():
                self._loop.call_soon(self._loop.stop)

    def close(self):
        with self._lock:
            self._closing = True
            if self._loop is None:
                self._closed.set()
                return
            if self._cleanup is None:
                self._cleanup = asyncio.run_coroutine_threadsafe(self._dispose(), self._loop)
            cleanup = self._cleanup
        try:
            cleanup.result(timeout=5)
        except Exception:
            pass  # The UI remains closing until cleanup is actually confirmed.
