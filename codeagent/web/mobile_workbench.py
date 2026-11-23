"""Authenticated HTTPS entry point for the existing workbench UI and its SSE streams."""
import asyncio
import logging
import secrets
import socket
import subprocess
import time
from contextlib import nullcontext

import uvicorn
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .mobile_tls import identity, lan_addresses, windows_wifi_address

COOKIE = "__Host-codeweft-mobile"
LOGIN = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CodeWeft</title>
<style>body{font:17px system-ui;margin:12vh 8vw;color:#282931}</style><h1>CodeWeft</h1>
<p id="message">正在连接工作台…</p><script>
const ticket=location.hash.slice(1);history.replaceState(null,'','/open');
(async()=>{if(ticket){
const response=await fetch('/open',{method:'POST',headers:{Authorization:'Bearer '+ticket}});
if(response.ok){const {chat}=await response.json();if(chat)localStorage.setItem('codeagent.conversation',chat);
location.replace('/');return}}
document.querySelector('#message').textContent='请从已配对手机的「完整工作台」重新进入。';
})().catch(()=>document.querySelector('#message').textContent='连接中断，请重新打开。');
</script></html>"""


class Address(BaseModel):
    address: str


class EmbeddedServer(uvicorn.Server):
    # The desktop server owns process signals and drives this listener's shutdown.
    def capture_signals(self):
        return nullcontext()

    def install_signal_handlers(self):
        pass


class FullWorkbench:
    def __init__(self, link, app, root, local_session, *, port=8766):
        self.link = link
        self.app = app
        self.root = root
        self.local_session = local_session
        self.port = port
        self.address = self.owner = self.ticket = self.session = None
        self.ca = self.server = self.task = None
        self.lock = asyncio.Lock()
        self.ticket_until = self.session_until = 0

    @property
    def enabled(self):
        return self.owner is not None and self.owner == self.link.device and self.address is not None

    def launch(self, chat=None):
        if not self.enabled:
            raise ValueError("请先在电脑的「设置 → 连接手机」开启完整工作台")
        self.ticket = secrets.token_urlsafe(32)
        self.chat = chat if isinstance(chat, str) and self.link.repo.get_conversation(chat) else None
        self.ticket_until = time.monotonic() + 60
        return {"url": self.address + "/open#" + self.ticket}

    async def start_on_wifi(self):
        """Restore access for the paired phone at desktop startup."""
        if self.link.device is None:
            return
        try:
            ip = await asyncio.to_thread(windows_wifi_address)
            if ip is not None:
                await self.start(ip)
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            logging.getLogger(__name__).warning("Unable to start mobile Wi-Fi workbench: %s", exc)

    async def start(self, ip):
        async with self.lock:
            if ip not in lan_addresses():
                raise ValueError("请选择本机的 Wi-Fi 或局域网地址")
            if self.link.device is None:
                raise ValueError("请先配对手机")
            origin = f"https://{ip}:{self.port}"
            if self.enabled and self.address == origin:
                return
            await self._stop()
            owner = self.link.device
            ca, leaf, key = identity(self.root, ip)
            config = uvicorn.Config(self.handle, lifespan="off", interface="asgi3", ws="none",
                                    access_log=False, log_config=None, proxy_headers=False,
                                    ssl_certfile=str(leaf), ssl_keyfile=str(key), timeout_graceful_shutdown=1)
            config.load()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind((ip, self.port))
                sock.listen()
                self.server = EmbeddedServer(config)
                self.task = asyncio.create_task(self.server.serve(sockets=[sock]))
                while not self.server.started:
                    if self.task.done():
                        await self.task
                        raise RuntimeError("手机监听器未能启动")
                    await asyncio.sleep(.01)
            except BaseException:
                sock.close()
                await self._stop()
                raise
            self.ca, self.address, self.owner = ca, f"https://{ip}:{sock.getsockname()[1]}", owner

    async def stop(self):
        async with self.lock:
            await self._stop()

    async def _stop(self):
        self.owner = self.address = self.session = self.ticket = None
        if self.server:
            self.server.should_exit = True
        if self.task:
            await self.task
        self.server = self.task = None

    async def handle(self, scope, receive, send):
        req = Request(scope, receive)
        origin = req.headers.get("origin")
        safe = req.method in {"GET", "HEAD"}
        path = scope["path"]
        allowed = (self.enabled and "https://" + req.headers.get("host", "") == self.address
                   and (origin == self.address or (safe and origin is None)))
        if not allowed:
            response = Response(status_code=403)
        elif path == "/open" and req.method == "GET":
            response = HTMLResponse(LOGIN)
        elif path == "/open" and req.method == "POST":
            header = req.headers.get("authorization", "").encode()
            with self.link.lock:
                valid = self.ticket and time.monotonic() < self.ticket_until and secrets.compare_digest(
                    header, ("Bearer " + self.ticket).encode())
                if valid:
                    self.ticket = None
                    self.session = secrets.token_urlsafe(32)
                    self.session_until = time.monotonic() + 8 * 3600
            if valid:
                response = JSONResponse({"chat": self.chat})
                response.set_cookie(COOKIE, self.session, secure=True, httponly=True, samesite="strict", max_age=28800)
            else:
                response = Response(status_code=403)
        elif (not self.session or time.monotonic() >= self.session_until or not secrets.compare_digest(
                req.cookies.get(COOKIE, "").encode(), self.session.encode())):
            response = HTMLResponse(LOGIN, status_code=401)
        elif path == "/api/mobile/status" and req.method == "GET":
            response = JSONResponse({"paired": True, "remote": True})
        elif path.startswith(("/api/mobile", "/mobile-link")) or not path.startswith("/") or "//" in path or ".." in path.split("/"):
            response = Response(status_code=403)
        else:
            # Adapt a verified session to the loopback app in process. No client
            # controls the upstream address, local cookie or authorization headers.
            session = self.session
            headers = [(k, v) for k, v in scope["headers"] if k.lower() in
                       {b"content-type", b"content-length", b"accept", b"last-event-id", b"range"}]
            headers.extend([(b"host", b"localhost"), (b"cookie", b"codeagent_session=" + self.local_session.encode())])
            async def deliver(message):
                if not self.enabled or self.session != session or time.monotonic() >= self.session_until:
                    raise OSError("Mobile session expired")
                if message["type"] == "http.response.start":
                    message["headers"] = [(k, v) for k, v in message["headers"] if k.lower() not in
                                          {b"set-cookie", b"cache-control"}] + [(b"cache-control", b"no-store"),
                                          (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer")]
                await send(message)
            await self.app(dict(scope, headers=headers, scheme="http", server=("localhost", 80)), receive, deliver)
            return
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"})
        await response(scope, receive, send)


def workbench_router(service):
    router = APIRouter(prefix="/api/mobile/workbench")

    @router.get("")
    def status():
        return {"enabled": service.enabled, "origin": service.address, "addresses": lan_addresses()}

    @router.post("")
    async def enable(body: Address):
        try:
            await service.start(body.address)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return status()

    @router.delete("")
    async def disable():
        await service.stop()
        return status()

    @router.get("/certificate")
    def certificate():
        if not service.enabled:
            raise HTTPException(409, "请先开启完整工作台")
        return FileResponse(service.ca, media_type="application/x-x509-ca-cert", filename="CodeWeft.cer")

    return router
