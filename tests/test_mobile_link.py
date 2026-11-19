import json
import time
from base64 import b64decode, b64encode
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from nacl.secret import SecretBox

from codeagent.web.mobile_link import MobileLink, mobile_router
from codeagent.web.storage import SQLiteRepository
from codeagent.web.workspaces import WorkspaceCatalog


def test_pair_browse_resume_and_revoke(tmp_path):
    submissions = []

    def submit(chat, content, **options):
        submissions.append(options)
        repo.create_message(chat, role="user", content=content)
        return SimpleNamespace(id="run1")

    with SQLiteRepository(tmp_path / "test.db") as repo:
        project = str(tmp_path)
        second = tmp_path / "second"
        second.mkdir()
        a = repo.create_conversation(title="A", workspace=project)
        b = repo.create_conversation(title="B", workspace=second)
        app = FastAPI()
        link = MobileLink(repo, SimpleNamespace(submit=submit), WorkspaceCatalog(project))
        app.include_router(mobile_router(link))
        client = TestClient(app)
        code = client.post("/api/mobile/invite").json()["code"]

        def exchange(key, body, *, pairing=False):
            box = SecretBox(b64decode(key))
            wire = box.encrypt(json.dumps(dict(direction="request", time=int(time.time()), **body)).encode())
            packet = {"pairing": pairing, "box": b64encode(wire).decode()}
            response = client.post("/mobile-link/exchange", json=packet)
            if response.status_code != 200:
                return response.status_code, packet
            plain = json.loads(box.decrypt(b64decode(response.json()["box"])))
            assert plain["request"] == b64encode(wire.nonce).decode()
            return plain, packet

        reply, invite = exchange(code, {}, pairing=True)
        key = reply["result"]["key"]
        assert client.post("/mobile-link/exchange", json=invite).status_code == 401
        reply, packet = exchange(key, {"action": "catalog"})
        assert {item["id"] for item in reply["result"]} == {a.id, b.id}
        assert client.post("/mobile-link/exchange", json=packet).status_code == 409
        command = dict(action="create", project=project, requestId=str(uuid4()))
        created = exchange(key, command)[0]["result"]
        assert exchange(key, command)[0]["result"] == created
        target = dict(chat=a.id, project=project)
        content = "继续"
        assert "result" in exchange(key, dict(target, action="send", content=content, requestId=str(uuid4())))[0]
        assert submissions[0]["read_only"] is True
        reply, _ = exchange(key, dict(target, action="history"))
        assert reply["result"]["messages"][0]["text"] == content
        assert "error" in exchange(key, dict(target, action="history", project=str(second)))[0]
        assert "error" in exchange(key, dict(command, project=str(tmp_path.parent)))[0]
        client.post("/api/mobile/revoke")
        assert exchange(key, {"action": "catalog"})[0] == 401
