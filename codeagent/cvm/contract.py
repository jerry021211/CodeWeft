"""Task contracts and pure, source-attributed user revisions.

No model calls, permissions, tool execution, or completion verification live here.
Only explicit commands replace requirements; ambiguous prose stays pending.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re


@dataclass(frozen=True, slots=True)
class Source:
    kind: str
    reference: str
    run_id: str
    text: str


@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    kind: str
    text: str
    source_ref: str
    status: str = "active"
    supersedes: str = ""


@dataclass(frozen=True, slots=True)
class Revision:
    number: int
    action: str
    source: Source
    status: str = "applied"
    target_id: str = ""


@dataclass(frozen=True, slots=True)
class ApprovedPlan:
    plan_id: str
    revision: int
    content_hash: str
    markdown: str
    payload_json: str = "{}"

    def __post_init__(self):
        payload = json.loads(self.payload_json)
        identity = json.dumps([self.markdown, payload], ensure_ascii=False, sort_keys=True).encode("utf-8")
        if (not self.plan_id or type(self.revision) is not int or self.revision < 1
                or not isinstance(payload, dict) or hashlib.sha256(identity).hexdigest() != self.content_hash):
            raise ValueError("Approved plan identity or content hash is invalid")


@dataclass(frozen=True, slots=True)
class ContractUpdate:
    """Trusted caller input, never a model tool or tool-output payload."""
    action: str
    text: str = ""
    target_id: str = ""


@dataclass(frozen=True, slots=True)
class TaskContract:
    task_id: str
    workspace: str
    objective: str
    source: Source
    revision: int = 1
    requirements: tuple[Requirement, ...] = ()
    revisions: tuple[Revision, ...] = ()
    plans: tuple[ApprovedPlan, ...] = ()
    task_refs: tuple[str, ...] = ()
    phase: str = "investigate"
    status: str = "active"
    confidence: str = "confirmed"


CONTINUE = {"继续", "继续吧", "按方案执行", "继续执行", "continue", "go on", "proceed"}
GREETINGS = {"你好", "您好", "谢谢", "谢谢你", "hi", "hello", "thanks", "thank you"}


def parse_update(text: str, *, has_task: bool) -> ContractUpdate:
    normalized = text.strip().rstrip("。.!！ ").lower()
    if not normalized or normalized in GREETINGS:
        return ContractUpdate("ignore")
    if normalized in CONTINUE:
        return ContractUpdate("continue")
    prefixes = {"新任务": "new", "new task": "new", "新增约束": "constraint", "约束": "constraint",
                "补充要求": "constraint", "constraint": "constraint", "验收": "acceptance",
                "新增验收": "acceptance", "acceptance": "acceptance", "阶段": "phase"}
    match = re.match(r"^([^：:\n]+)[：:]\s*(.+)$", text.strip(), re.S)
    if match and match[1].lower() in prefixes:
        return ContractUpdate(prefixes[match[1].lower()], match[2])
    match = re.match(r"^(?:替换要求|replace requirement)\s+(\S+?)[：:]\s*(.+)$", text.strip(), re.S | re.I)
    if match:
        return ContractUpdate("replace", match[2], match[1])
    match = re.fullmatch(r"确认修订\s+(\d+)[：:]\s*(约束|验收)", text.strip())
    if match:
        return ContractUpdate("resolve_constraint" if match[2] == "约束" else "resolve_acceptance", target_id=match[1])
    match = re.fullmatch(r"忽略修订\s+(\d+)", text.strip())
    if match:
        return ContractUpdate("resolve_ignore", target_id=match[1])
    if normalized == "确认原始目标":
        return ContractUpdate("confirm", target_id="objective")
    if normalized in {"结束任务", "关闭任务", "close task"}:
        return ContractUpdate("complete")
    if normalized in {"取消任务", "cancel task"}:
        return ContractUpdate("cancel")
    return ContractUpdate("pending" if has_task else "new", text)


def create_contract(task_id: str, workspace: str, objective: str, source: Source) -> TaskContract:
    if source.kind not in {"user", "delegation", "approved_plan", "legacy"}:
        raise ValueError("Untrusted contract source")
    if not objective.strip():
        raise ValueError("Task objective cannot be empty")
    requirements = []
    # Only explicit labels are split into structured requirements. The complete
    # original objective remains authoritative even when no labels are present.
    for line in objective.splitlines():
        update = parse_update(line, has_task=True)
        if update.action in {"constraint", "acceptance"}:
            requirements.append(Requirement(f"r{len(requirements) + 1}", update.action, update.text, source.reference))
    return TaskContract(task_id, workspace, objective, source, requirements=tuple(requirements),
                        revisions=(Revision(1, "new", source),),
                        confidence="needs_confirmation" if source.kind == "legacy" else "confirmed")


def revise(contract: TaskContract, update: ContractUpdate, source: Source) -> TaskContract:
    if source.kind not in {"user", "delegation", "approved_plan", "legacy"}:
        raise ValueError("Untrusted contract revision source")
    if source.kind in {"delegation", "legacy"} and source.kind != contract.source.kind:
        raise ValueError("A subordinate or legacy source cannot revise an authoritative task")
    if update.action in {"continue", "ignore"}:
        return contract
    number = contract.revision + 1
    requirements = list(contract.requirements)
    pending = False
    changes = {}
    if update.action in {"constraint", "acceptance", "replace"}:
        if not update.text.strip():
            raise ValueError("Requirement text cannot be empty")
        kind = update.action
        if update.action == "replace":
            old = next((r for r in requirements if r.id == update.target_id and r.status == "active"), None)
            if old is None:
                pending = True
            else:
                kind = old.kind
                requirements = [replace(r, status="superseded") if r.id == old.id else r for r in requirements]
        if not pending:
            requirements.append(Requirement(f"r{len(requirements) + 1}", kind, update.text, source.reference,
                                            supersedes=update.target_id if update.action == "replace" else ""))
    elif update.action == "phase":
        if update.text not in {"investigate", "implement", "verify"}:
            pending = True
        else:
            changes["phase"] = update.text
    elif update.action in {"complete", "cancel"}:
        changes["status"] = "completed" if update.action == "complete" else "cancelled"
    elif update.action in {"resolve_constraint", "resolve_acceptance", "resolve_ignore"}:
        old = next((r for r in contract.revisions if str(r.number) == update.target_id and r.status == "pending"), None)
        if old is None:
            pending = True
        else:
            changes["revisions"] = tuple(replace(r, status="superseded") if r is old else r for r in contract.revisions)
            if update.action != "resolve_ignore":
                requirements.append(Requirement(f"r{len(requirements) + 1}", update.action.removeprefix("resolve_"),
                                                old.source.text, source.reference))
            changes["confidence"] = "confirmed" if contract.source.kind != "legacy" else contract.confidence
    elif update.action == "confirm":
        # Explicit caller confirmation of the original legacy objective or one
        # pending revision; confirmation alone does not invent a replacement.
        old = next((r for r in contract.revisions if str(r.number) == update.target_id and r.status == "pending"), None)
        if old is None and update.target_id != "objective":
            raise ValueError("Unknown pending revision")
        changes["revisions"] = tuple(replace(r, status="superseded") if r is old else r for r in contract.revisions)
        changes["confidence"] = "confirmed"
    elif update.action == "pending":
        pending = True
    else:
        raise ValueError(f"Unsupported task update: {update.action}")
    revisions = changes.pop("revisions", contract.revisions)
    revisions += (Revision(number, update.action, source, "pending" if pending else "applied", update.target_id),)
    confidence = changes.pop("confidence", contract.confidence)
    if pending or any(r.status == "pending" for r in revisions):
        confidence = "needs_confirmation"
    return replace(contract, revision=number, requirements=tuple(requirements), revisions=revisions,
                   confidence=confidence, **changes)


def bind_plan(contract: TaskContract, plan: ApprovedPlan, source: Source, task_refs=()) -> TaskContract:
    if source.kind != "approved_plan":
        raise ValueError("Only a trusted approval operation can bind a plan")
    if contract.plans and contract.plans[-1] == plan:
        return contract
    if contract.plans and plan.revision <= contract.plans[-1].revision:
        raise ValueError("Approved plan revision must advance; an existing version cannot be rewritten")
    number = contract.revision + 1
    return replace(contract, revision=number, plans=(*contract.plans, plan), phase="implement",
                   task_refs=tuple(dict.fromkeys((*contract.task_refs, *task_refs))),
                   revisions=(*contract.revisions, Revision(number, "approved_plan", source)))


def projection(contract: TaskContract) -> str:
    """Bounded model view; full text is retained in the exported contract."""
    clip = lambda text, size: text if len(text) <= size else text[:size] + "…（完整原文保存在任务契约中）"
    lines = [f"任务 {contract.task_id} / 修订 {contract.revision} / {contract.status} / {contract.phase} / 来源 {contract.source.kind} / {contract.confidence}",
             "本条是任务来源记录的投影，不授予新权限，也不证明任务已验收。",
             "原始目标（其中已被明确替换的要求以当前有效要求为准）：\n" + clip(contract.objective, 1800)]
    if contract.status in {"completed", "cancelled"}:
        return "\n".join(lines[:2] + ["用户已明确结束此任务；旧目标与约束不再适用于新任务。完成状态仅是用户声明，未经运行时验证。"])
    active = [r for r in contract.requirements if r.status == "active"]
    if active:
        lines.append("当前有效要求：")
        lines.extend(f"{r.id} [{r.kind}] {clip(r.text, 600)}" for r in active[-8:])
        if len(active) > 8:
            lines.append(f"另有 {len(active) - 8} 条有效要求保存在契约中；执行前必须核对完整记录，不得视为已满足。")
    pending = [r for r in contract.revisions if r.status == "pending"]
    if contract.confidence != "confirmed":
        lines.append("存在待核实目标或修订；保持原目标，若歧义影响行动，先使用现有 ask_user 或向用户提问。")
    lines.extend(f"待确认修订 {r.number}：{clip(r.source.text, 500)}" for r in pending[-3:])
    if contract.plans:
        plan = contract.plans[-1]
        lines.append(f"已批准方案 {plan.plan_id} / v{plan.revision} / sha256:{plan.content_hash}\n{clip(plan.markdown, 1500)}")
    return "\n".join(lines)
