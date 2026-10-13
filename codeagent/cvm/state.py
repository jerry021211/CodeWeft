"""Versioned contract state; unknown or corrupt checkpoints remain opaque."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
import json
from uuid import uuid4

from codeagent.cvm.config import CvmConfig
from codeagent.cvm.contract import (ApprovedPlan, ContractUpdate, Requirement, Revision, Source,
                                    TaskContract, bind_plan, create_contract, parse_update, projection, revise)
from codeagent.messages import extract_text
from codeagent.cvm.evidence import EvidenceLedger


def workspace_key(workspace) -> str:
    import os
    return os.path.normcase(str(Path(workspace).resolve()))


def approved_execution_prompt(plan: ApprovedPlan) -> str:
    return '执行用户已批准的方案（版本 ' + str(plan.revision) + '）：\n\n' + plan.markdown


def _decode_contract(data) -> TaskContract:
    values = dict(data)
    values["source"] = Source(**values["source"])
    values["requirements"] = tuple(Requirement(**item) for item in values["requirements"])
    values["revisions"] = tuple(Revision(**{**item, "source": Source(**item["source"])}) for item in values["revisions"])
    values["plans"] = tuple(ApprovedPlan(**item) for item in values["plans"])
    values["task_refs"] = tuple(values["task_refs"])
    contract = TaskContract(**values)
    if (not contract.task_id or not isinstance(contract.objective, str) or not contract.objective.strip()
            or not isinstance(contract.workspace, str) or type(contract.revision) is not int
            or contract.revision < 1 or contract.phase not in {"investigate", "implement", "verify"}
            or contract.status not in {"active", "blocked", "completed", "superseded", "cancelled"}
            or contract.confidence not in {"confirmed", "needs_confirmation"}):
        raise ValueError("Invalid contract fields")
    sources = [contract.source, *(r.source for r in contract.revisions)]
    if any(s.kind not in {"user", "legacy", "delegation", "approved_plan"}
           or any(not isinstance(x, str) for x in (s.reference, s.run_id, s.text)) for s in sources):
        raise ValueError("Invalid source provenance")
    if (not contract.revisions or [r.number for r in contract.revisions] != list(range(1, contract.revision + 1))
            or any(r.status not in {"applied", "pending", "superseded"} for r in contract.revisions)):
        raise ValueError("Invalid revision history")
    ids = {r.id for r in contract.requirements}
    if len(ids) != len(contract.requirements) or any(
        r.kind not in {"constraint", "acceptance"} or r.status not in {"active", "superseded"}
        or not isinstance(r.text, str) or not r.text.strip() or r.source_ref not in {s.reference for s in sources}
        or (r.supersedes and r.supersedes not in ids) for r in contract.requirements
    ) or any(not isinstance(ref, str) for ref in contract.task_refs):
        raise ValueError("Invalid requirement references")
    return contract


class CvmRuntime:
    def __init__(self, config: CvmConfig, workspace, *, source_kind="user", task_refs=()):
        self.config = config
        self.workspace = workspace_key(workspace)
        self.source_kind = source_kind
        self.task_refs = tuple(task_refs)
        self.current: TaskContract | None = None
        self.archived: tuple[TaskContract, ...] = ()
        self.diagnostics: list[str] = []
        self._opaque = None
        self._preserve_opaque = False
        self._approved_prompt = None
        self.evidence = EvidenceLedger(self.workspace)
        self._saved_evidence = None
        self.supervision = {}

    @property
    def recording(self):
        return self.config.evidence == 'shadow' and not self._preserve_opaque

    def refresh_evidence(self):
        if self.recording:
            try:
                self.evidence.refresh()
            except Exception:
                self.evidence.fail('snapshot_refresh_failed')

    @property
    def observing(self):
        return self.config.contract != "off" and not self._preserve_opaque

    @property
    def active(self):
        return self.config.contract == "active" and not self._preserve_opaque

    def receive(self, text: str, *, run_id: str, reference: str, update: ContractUpdate | None = None):
        if not self.observing:
            return
        if self._approved_prompt == text:
            self._approved_prompt = None
            return
        source = Source(self.source_kind, reference, run_id, text)
        if source.kind not in {"user", "delegation"}:
            raise ValueError("Only user input or a local delegation can revise a contract")
        current = self.current
        has_task = current is not None and current.status in {"active", "blocked"}
        update = update or parse_update(text, has_task=has_task)
        if update.action in {"ignore", "continue"}:
            return
        if update.action == "new":
            created = replace(create_contract(uuid4().hex, self.workspace, update.text, source),
                              task_refs=self.task_refs)
            if current is not None:
                self.archived += (replace(current, status="superseded") if has_task else current,)
            self.current = created
        elif has_task:
            self.current = revise(current, update, source)
        else:
            # A standalone constraint is not a new engineering task.
            self.diagnostics.append("revision_without_active_task")
        if self.recording and self.current is not None and source.kind == 'user':
            self.evidence.receive_definitions(text, source_ref=reference, source_kind='user',
                                              task_id=self.current.task_id)

    def approve(self, plan: ApprovedPlan, *, run_id: str, task_refs=()):
        if not self.observing:
            return
        source = Source("approved_plan", f"plan:{plan.plan_id}:{plan.revision}", run_id, plan.markdown)
        if self.current is None or self.current.status not in {"active", "blocked"}:
            if self.current is not None:
                self.archived += (self.current,)
            self.current = create_contract(uuid4().hex, self.workspace, plan.markdown, source)
        self.current = bind_plan(self.current, plan, source, task_refs)
        self._approved_prompt = approved_execution_prompt(plan)
        if self.recording:
            self.evidence.receive_definitions(plan.markdown, source_ref=source.reference,
                                              source_kind='approved_plan', task_id=self.current.task_id)

    def reminder(self):
        return projection(self.current) if self.active and self.current else ""

    def user_goal(self):
        if not self.active or self.current is None:
            return None
        if self.current.status in {"completed", "cancelled", "superseded"}:
            return ""
        requirements = [f"{r.id}: {r.text}" for r in self.current.requirements if r.status == "active"]
        return self.current.objective + ("\n当前有效要求（替换项覆盖旧表述）：\n" + "\n".join(requirements) if requirements else "")

    def export(self):
        if self._preserve_opaque:
            return deepcopy(self._opaque)
        if self.config.contract == "off" and self.current is None and not self.archived:
            return None
        evidence = self.evidence.export() if self.recording else self._saved_evidence
        if evidence is None and (self.evidence.observations or self.evidence.definitions or self.evidence.degraded):
            evidence = self.evidence.export()
        return json.loads(json.dumps({"schema_version": 2 if evidence is not None else 1, "policy_version": 1, "workspace": self.workspace,
                "config": self.config.snapshot(), "current": asdict(self.current) if self.current else None,
                "archived": [asdict(item) for item in self.archived], "diagnostics": list(self.diagnostics),
                **({'supervision': self.supervision} if self.supervision else {}),
                **({'evidence': evidence} if evidence is not None else {})}, ensure_ascii=False))

    def restore(self, payload, *, messages=()):
        self.current, self.archived = None, ()
        self.diagnostics = []
        self._opaque, self._preserve_opaque = None, False
        self._approved_prompt = None
        self.evidence = EvidenceLedger(self.workspace)
        self._saved_evidence = None
        self.supervision = {}
        if payload is None:
            if self.observing:
                self._restore_legacy(messages)
            return
        if self.config.contract == "off":
            # Rollback preserves the original packet, without interpreting it.
            self._opaque, self._preserve_opaque = deepcopy(payload), True
            return
        try:
            if (type(payload) is not dict or type(payload.get("schema_version")) is not int
                    or type(payload.get("policy_version")) is not int
                    or payload.get("schema_version") not in {1, 2} or payload.get("policy_version") != 1):
                raise ValueError("unsupported_schema")
            if payload.get("workspace") != self.workspace:
                raise ValueError("workspace_mismatch")
            current = _decode_contract(payload["current"]) if payload["current"] is not None else None
            archived = tuple(_decode_contract(item) for item in payload["archived"])
            if any(item.workspace != self.workspace for item in (*archived, *((current,) if current else ()))):
                raise ValueError("workspace_mismatch")
            if any(not isinstance(item, str) for item in payload["diagnostics"]):
                raise ValueError("invalid_diagnostics")
            self.current, self.archived = current, archived
            self.diagnostics = list(payload["diagnostics"])
            if 'supervision' in payload:
                from codeagent.cvm.supervision import restore_supervision
                self.supervision = restore_supervision(payload['supervision'])
            if payload['schema_version'] == 2:
                if self.recording:
                    self.evidence.restore(payload['evidence'])
                else:
                    self._saved_evidence = deepcopy(payload['evidence'])
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            self._opaque, self._preserve_opaque = deepcopy(payload), True
            self.diagnostics = [f"cvm_restore_disabled:{error}"]

    def _restore_legacy(self, messages):
        # Legacy messages have no reliable provenance tag. Use candidates only,
        # never summaries, tool results, or a restored user_goal as authority.
        for index, message in enumerate(messages):
            if message.get("role") != "user" or message.get("_context_source") == "runtime":
                continue
            content = message.get("content")
            if isinstance(content, list) and any(b.get("type") == "tool_result" for b in content if isinstance(b, dict)):
                continue
            text = extract_text(content)
            if not text.strip() or text.lstrip().startswith(("[", "<")):
                continue
            update = parse_update(text, has_task=self.current is not None)
            if self.current is None:
                if update.action != "new":
                    continue
                source = Source("legacy", f"history:{index}", "", text)
                self.current = create_contract(uuid4().hex, self.workspace, update.text, source)
            elif update.action not in {"ignore", "continue"}:
                source = Source("legacy", f"history:{index}", "", text)
                self.current = revise(self.current, ContractUpdate("pending", text), source)
        if self.current:
            self.diagnostics.append("legacy_contract_needs_confirmation")
