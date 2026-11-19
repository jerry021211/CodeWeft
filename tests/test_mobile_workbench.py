import asyncio
import ssl
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import patch

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from codeagent.web.mobile_workbench import FullWorkbench
from codeagent.web.mobile_link import MobileLink


def test_authenticated_https_streams_and_mutations(tmp_path):
    async def check():
        app = FastAPI()

        @app.get("/api/events")
        async def events():
            return StreamingResponse(iter(["data: hello\n\n"]), media_type="text/event-stream")

        @app.post("/api/runs")
        async def run(request: Request):
            assert request.cookies == {"codeagent_session": "fixture"}
            assert request.url.hostname == "localhost"
            return {"ok": True}

        link = MobileLink(SimpleNamespace(get_conversation=lambda _: True), None, None)
        link.device = b"paired"
        gate = FullWorkbench(link, app, tmp_path, "fixture", port=0)
        with patch("codeagent.web.mobile_workbench.lan_addresses", return_value=["127.0.0.1"]):
            await gate.start("127.0.0.1")
        try:
            context = ssl.create_default_context(cadata=gate.ca.read_bytes())
            async with httpx.AsyncClient(base_url=gate.address, verify=context, trust_env=False) as client:
                assert (await client.get("/api/events")).status_code == 401
                ticket = urlsplit(gate.launch("chat")["url"]).fragment
                headers = {"Origin": gate.address, "Authorization": "Bearer " + ticket}
                login = await client.post("/open", headers=headers)
                assert login.json() == {"chat": "chat"}
                assert "HttpOnly" in login.headers["set-cookie"]
                assert (await client.post("/open", headers=headers)).status_code == 403
                assert (await client.post("/api/runs", headers={"Origin": "https://other.invalid"})).status_code == 403
                assert (await client.post("/api/runs")).status_code == 403
                assert (await client.post("/api/runs", json={}, headers={"Origin": gate.address})).json()["ok"]
                assert (await client.get("/api/events")).text == "data: hello\n\n"
                assert (await client.get("/api/mobile/invite")).status_code == 403
                link.revoke()
                assert (await client.get("/api/events")).status_code == 403
        finally:
            await gate.stop()
    asyncio.run(check())
