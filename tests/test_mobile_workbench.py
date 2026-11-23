import asyncio
import os
import ssl
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from codeagent.web.mobile_workbench import FullWorkbench
from codeagent.web.mobile_link import MobileLink
from codeagent.web.mobile_tls import windows_wifi_address


@pytest.mark.skipif(os.name != "nt", reason="Windows adapter discovery")
def test_wifi_selection_excludes_other_adapters_and_ambiguous_addresses():
    lan = ["172.16.0.1", "192.168.1.10", "192.168.2.10"]
    with patch("codeagent.web.mobile_tls.lan_addresses", return_value=lan), \
         patch("codeagent.web.mobile_tls.subprocess.run") as discover:
        discover.return_value.stdout = b"fe80::1\r\n192.168.1.10\r\n"
        assert windows_wifi_address() == "192.168.1.10"
        discover.return_value.stdout += b"192.168.2.10\r\n"
        assert windows_wifi_address() is None
        discover.return_value.stdout = b""
        assert windows_wifi_address() is None


def test_startup_requires_pairing_and_wifi_and_tolerates_bind_failure(tmp_path):
    async def check():
        link = SimpleNamespace(device=None)
        gate = FullWorkbench(link, None, tmp_path, "fixture")
        with patch("codeagent.web.mobile_workbench.windows_wifi_address") as discover, \
             patch.object(gate, "start", new_callable=AsyncMock) as start:
            await gate.start_on_wifi()
            discover.assert_not_called()
            link.device = b"paired"
            discover.return_value = None
            await gate.start_on_wifi()
            start.assert_not_awaited()
            discover.return_value = "192.168.1.10"
            start.side_effect = OSError("port in use")
            await gate.start_on_wifi()
            start.assert_awaited_once_with("192.168.1.10")
    asyncio.run(check())


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
        link.pairing.replace(b"paired")
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
