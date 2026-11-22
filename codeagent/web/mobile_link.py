"""One trusted handset per workbench data directory."""
import json
import secrets
import socket
import time
from base64 import b64encode, b64decode
from threading import RLock
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from codeagent.web.storage import StorageError
from codeagent.web.mobile_pairing import PairingStore


class Packet(BaseModel):
    pairing: bool = False
    box: str = Field(min_length=56, max_length=160000)


def encoded(data):
    return b64encode(bytes(data)).decode("ascii")


class MobileLink:
    def __init__(self, repo, scheduler, catalog, *, pairing_dir=None):
        self.repo = repo
        self.scheduler = scheduler
        self.catalog = catalog
        self.lock = RLock()
        self.pairing = PairingStore(pairing_dir)
        self.ticket = None
        self.deadline = 0
        self.workbench = None

    @property
    def device(self):
        return self.pairing.read()

    def invite(self):
        from nacl.secret import SecretBox
        with self.lock:
            self.ticket = secrets.token_bytes(SecretBox.KEY_SIZE)
            self.deadline = time.monotonic() + 300
            return {"code": encoded(self.ticket)}

    def revoke(self):
        with self.lock:
            self.pairing.replace(None)
            self.ticket = None

    def exchange(self, packet):
        from nacl.exceptions import CryptoError
        from nacl.secret import SecretBox
        with self.lock:
            key = self.ticket if packet.pairing and time.monotonic() < self.deadline else None
            if not packet.pairing:
                key = self.device
            if not key:
                raise HTTPException(401, "请在电脑上重新生成配对码")
            box = SecretBox(key)
            try:
                wire = b64decode(packet.box, validate=True)
                data = json.loads(box.decrypt(wire))
            except (ValueError, CryptoError, UnicodeError):
                raise HTTPException(401, "配对信息无效") from None
            now = time.time()
            if (not isinstance(data, dict) or data.get("direction") != "request"
                    or not isinstance(data.get("time"), int) or abs(now - data["time"]) > 120):
                raise HTTPException(400, "请检查手机和电脑的时间")
            nonce = encoded(wire[:SecretBox.NONCE_SIZE])
            if not packet.pairing and not self.pairing.accept(key, nonce, now):
                raise HTTPException(409, "请求已接收")
            try:
                if packet.pairing:
                    device = secrets.token_bytes(SecretBox.KEY_SIZE)
                    self.pairing.replace(device)
                    self.ticket = None
                    result = {"key": encoded(device), "computer": socket.gethostname()}
                else:
                    result = self.dispatch(data)
                reply = {"result": result}
            except (ValueError, StorageError) as exc:
                reply = {"error": str(exc)}
            reply["request"] = nonce
            return {"box": encoded(box.encrypt(json.dumps(reply, ensure_ascii=False).encode()))}

    def conversations(self):
        offset = 0
        while True:
            batch = self.repo.list_conversations(limit=500, offset=offset)
            yield from batch
            if len(batch) < 500:
                break
            offset += len(batch)

    def dispatch(self, data):
        action = data.get("action")
        if action == "open_workbench" and self.workbench:
            return self.workbench.launch(data.get("chat"))
        project = data.get("project")
        if action == "catalog":
            return [{"id": c.id, "project": c.workspace, "title": c.title}
                    for c in self.conversations() if c.workspace]
        if action == "create":
            if not any(c.workspace == project for c in self.conversations()):
                raise ValueError("项目已移除，请刷新")
            workspace = self.catalog.resolve(project)
            identifier = "conv_mobile_" + str(UUID(str(data.get("requestId"))))
            existing = self.repo.get_conversation(identifier)
            if existing and (existing.workspace != str(workspace) or existing.archived_at):
                raise ValueError("此请求对应其他会话")
            chat = existing or self.repo.create_conversation(title="新对话", workspace=workspace, conversation_id=identifier)
            return {"id": chat.id, "project": chat.workspace, "title": chat.title}

        chat = self.repo.get_conversation(str(data.get("chat")))
        if not chat or chat.archived_at or chat.workspace != project:
            raise ValueError("会话已移除或不属于当前项目")
        control = bool(self.workbench and self.workbench.enabled)
        if action in {"cancel", "decision"}:
            if not control:
                raise ValueError("请先在电脑授权完整工作台")
            run = self.repo.get_run(str(data.get("run")))
            if not run or run.conversation_id != chat.id:
                raise ValueError("任务不属于当前会话")
            if action == "cancel":
                return {"status": self.scheduler.cancel(run.id).status}
            approval = self.repo.get_approval(str(data.get("approval")))
            decision = data.get("decision")
            if not approval or approval.run_id != run.id or decision not in ("allow", "deny"):
                raise ValueError("审批请求无效")
            return {"status": self.scheduler.resolve_approval(run.id, approval.id, decision).status}
        if action == "send":
            content = data.get("content")
            if not isinstance(content, str) or not 0 < len(content.strip()) <= 20000:
                raise ValueError("消息长度应为 1 至 20000 个字符")
            if self.repo.planning_state(chat.id)["mode"] == "planning":
                raise ValueError("请在电脑上完成当前方案，或新建对话")
            request_id = UUID(str(data.get("requestId")))
            # 网络超时后手机会换 nonce 重发；同一次提问复用 requestId，避免电脑再次执行。
            execute = data.get("execute") is True
            if execute and not control:
                raise ValueError("请先在电脑授权完整工作台")
            if execute and self.repo.get_active_team_run_for_conversation(chat.id):
                raise ValueError("请在完整工作台中继续团队任务")
            run = self.scheduler.submit(chat.id, content, read_only=not execute, approval_mode="ask",
                                        admission_key=f"mobile:{chat.id}:{request_id}")
            return {"run": run.id}
        if action != "history":
            raise ValueError("未知手机操作")
        messages = []
        offset = int(data.get("offset", 0))
        if not 0 <= offset < 2**31:
            raise ValueError("无效的历史位置")
        batch = self.repo.list_messages(chat.id, limit=100, offset=offset)
        for msg in batch:
            if msg.role not in {"user", "assistant"}:
                continue
            content = msg.content
            if isinstance(content, list):
                content = "\n".join(str(p.get("text", "")) for p in content
                                    if isinstance(p, dict) and p.get("type") == "text")
            if isinstance(content, str) and content:
                messages.append({"id": msg.id, "role": msg.role, "text": content})
        latest = self.repo.list_runs(conversation_id=chat.id, limit=1)
        active = self.repo.list_runs(conversation_id=chat.id, statuses=["running", "queued"], limit=1)
        approvals = self.scheduler.list_pending_approvals(chat.id) if control else []
        current = next(iter(active or latest), None)
        return {"messages": messages, "status": current.status if current else None, "control": control,
                "run": active[0].id if active else None,
                "approvals": [{"id": a.id, "run": a.run_id, "tool": a.tool_name, "input": a.tool_input,
                               "reason": a.reason} for a in approvals],
                "next": offset + len(batch), "more": len(batch) == 100}


def mobile_router(link):
    router = APIRouter()

    @router.get("/api/mobile/status")
    def status():
        return {"paired": link.device is not None}

    @router.post("/api/mobile/invite")
    def invite():
        return link.invite()

    @router.post("/api/mobile/revoke")
    async def revoke():
        link.revoke()
        if link.workbench:
            await link.workbench.stop()
        return {"paired": False}

    @router.post("/mobile-link/exchange")
    def exchange(packet: Packet):
        return link.exchange(packet)

    return router
