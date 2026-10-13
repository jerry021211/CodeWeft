"""Small Runtime-backed collaboration tools for Team roles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from codeagent.legacy.teams.bus import MessageBus
from codeagent.legacy.teams.candidates import CandidateService
from codeagent.legacy.teams.models import TaskAttemptRecord, integration_summary
from codeagent.tools.base import ToolDefinition
from codeagent.legacy.teams.worktrees import WorktreeManager


@dataclass(slots=True)
class SubmitAttemptPlanTool:
    repository: Any
    worktrees: WorktreeManager
    attempt: TaskAttemptRecord
    yield_callback: Callable[[str], None]
    definition: ToolDefinition = ToolDefinition(
        name="team_submit_attempt_plan",
        description=(
            "Submit a new immutable execution-plan revision when the assigned "
            "Attempt requires approval, then pause for Lead review."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "planned_files": {"type": "array", "items": {"type": "string"}},
                "planned_commands": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "planned_tests": {"type": "array", "items": {"type": "string"}},
                "write_scopes": {"type": "array", "items": {"type": "string"},
                                 "description": "Repository-relative paths, optionally directory/**; must stay within the task's approved scope. Other glob patterns are not supported."},
                "risk_level": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                },
            },
            "required": [
                "summary",
                "planned_files",
                "planned_commands",
                "planned_tests",
                "write_scopes",
                "risk_level",
            ],
        },
    )

    def run(
        self,
        summary: str,
        planned_files: list[str],
        planned_commands: list[str],
        planned_tests: list[str],
        write_scopes: list[str],
        risk_level: str,
    ) -> str:
        binding = self.repository.get_attempt_worktree_binding(self.attempt.id)
        if binding is None:
            return "Error: Attempt has no Worktree binding"
        self.worktrees.validate_binding(binding.id)
        suffix = uuid4().hex
        plan = self.repository.create_attempt_plan(
            self.attempt.id,
            summary=summary,
            planned_files=planned_files,
            planned_commands=planned_commands,
            planned_tests=planned_tests,
            write_scopes=write_scopes,
            risk_level=risk_level,
            created_by=self.attempt.agent_id,
            command_id=f"teammate-create-plan:{self.attempt.id}:{suffix}",
        )
        submitted = self.repository.submit_attempt_plan(
            self.attempt.id,
            plan.revision,
            command_id=f"teammate-submit-plan:{self.attempt.id}:{plan.revision}",
        )
        self.yield_callback("attempt_plan_submitted")
        return (
            f"Submitted Attempt Plan revision p{submitted.revision}; "
            "write tools remain disabled pending approval."
        )


@dataclass(slots=True)
class SubmitCandidateTool:
    service: CandidateService
    attempt: TaskAttemptRecord
    yield_callback: Callable[[str], None]
    definition: ToolDefinition = ToolDefinition(
        name="team_submit_candidate",
        description=(
            "Freeze and submit the current Worktree diff as a Candidate for Lead "
            "review. This revokes Teammate write access."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "tests_reported": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "known_risks": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "tests_reported", "known_risks"],
        },
    )

    def run(
        self,
        summary: str,
        tests_reported: list[str],
        known_risks: list[str],
    ) -> str:
        candidate = self.service.submit(
            self.attempt.id,
            summary=summary,
            tests_reported=tests_reported,
            known_risks=known_risks,
        )
        self.yield_callback("candidate_submitted")
        return f"Submitted frozen Candidate {candidate.id} for Lead review."


@dataclass(slots=True)
class SubmitAnalysisResultTool:
    repository: Any
    attempt: TaskAttemptRecord
    definition: ToolDefinition = ToolDefinition(
        name="team_submit_analysis_result",
        description=(
            "Submit the final result of a read-only analysis Task. Analysis Tasks "
            "do not create a Worktree or candidate commit."
        ),
        input_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    )

    def run(self, summary: str) -> str:
        completed = self.repository.complete_analysis_attempt(
            self.attempt.id,
            summary=summary,
            submitted_by=self.attempt.agent_id,
            command_id=f"analysis-result:{self.attempt.id}",
        )
        return f"Analysis result submitted; Attempt {completed.id} succeeded."


@dataclass(slots=True)
class TeamProgressTool:
    repository: Any
    attempt: TaskAttemptRecord
    definition: ToolDefinition = ToolDefinition(
        name="team_report_progress",
        description="Report concise progress to the Team Lead through durable messaging.",
        input_schema={
            "type": "object",
            "properties": {
                "stage": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["stage", "summary"],
        },
    )

    def run(self, stage: str, summary: str) -> str:
        team = self.repository.get_team_run(self.attempt.team_run_id)
        assert team is not None
        lead_generation = _active_lead_generation(self.repository, team)
        message = MessageBus(self.repository).send(
            team.id,
            sender_type="teammate",
            sender_agent_id=self.attempt.agent_id,
            recipient_type="lead",
            recipient_agent_id=team.lead_agent_id,
            recipient_generation=lead_generation,
            message_type="PROGRESS",
            payload={"stage": stage, "summary": summary},
            dedupe_key=f"progress:{self.attempt.id}:{uuid4().hex}",
            task_id=self.attempt.task_id,
            attempt_id=self.attempt.id,
        )
        return f"Progress message persisted: {message.id}"


@dataclass(slots=True)
class TeamQuestionTool:
    repository: Any
    attempt: TaskAttemptRecord
    yield_callback: Callable[[str], None]
    definition: ToolDefinition = ToolDefinition(
        name="team_ask_lead",
        description="Ask the Team Lead a scoped question through durable messaging.",
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "blocking": {"type": "boolean"},
            },
            "required": ["question", "blocking"],
        },
    )

    def run(self, question: str, blocking: bool) -> str:
        team = self.repository.get_team_run(self.attempt.team_run_id)
        assert team is not None
        session = self.repository.get_agent_session(self.attempt.session_id)
        lead_generation = _active_lead_generation(self.repository, team)
        message = MessageBus(self.repository).send(
            team.id,
            sender_type="teammate",
            sender_agent_id=self.attempt.agent_id,
            recipient_type="lead",
            recipient_agent_id=team.lead_agent_id,
            recipient_generation=lead_generation,
            message_type="QUESTION",
            payload={
                "question": question, "blocking": bool(blocking),
                "session_id": session.id, "generation": session.generation,
            },
            dedupe_key=f"question:{self.attempt.id}:{uuid4().hex}",
            task_id=self.attempt.task_id,
            attempt_id=self.attempt.id,
        )
        if blocking:
            self.yield_callback(f"waiting_for_lead_answer:{message.id}")
        return f"Question persisted: {message.id}"


@dataclass(slots=True)
class TeamStatusTool:
    repository: Any
    team_run_id: str
    allow_code: bool
    definition: ToolDefinition = ToolDefinition(
        name="team_get_status",
        description="Read the durable state of the active TeamRun and its pending decisions.",
        input_schema={"type": "object", "properties": {}},
    )

    def run(self) -> str:
        team = self.repository.get_team_run(self.team_run_id)
        if team is None:
            return "Error: TeamRun no longer exists"
        attempts = self.repository.list_task_attempts(team.id)
        payload = {
            "team": team.to_dict(),
            "integrations": [integration_summary(r) for r in self.repository.list_team_integrations(team.id)],
            "plans": [
                item.to_dict()
                for item in self.repository.list_team_plan_revisions(team.id)
            ],
            "tasks": [
                item.to_dict(camel_case=True)
                for item in self.repository.list_task_resources(team.task_list_id)
            ],
            "scheduling": [
                item.to_dict()
                for item in self.repository.list_task_scheduling(
                    team.id, allow_code=self.allow_code
                )
            ],
            "sessions": [
                item.to_dict() for item in self.repository.list_agent_sessions(team.id)
            ],
            "attempts": [item.to_dict() for item in attempts],
            "attempt_plans": [
                item.to_dict()
                for attempt in attempts
                for item in self.repository.list_attempt_plans(attempt.id)
            ],
            "candidates": [
                item.to_dict() for item in self.repository.list_candidates(team.id)
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)


@dataclass(slots=True)
class TeamCandidateContextTool:
    repository: Any
    worktrees: WorktreeManager
    team_run_id: str
    definition: ToolDefinition = ToolDefinition(
        name="team_get_candidate_context",
        description="Read one frozen Candidate's metadata and patch for semantic review.",
        input_schema={
            "type": "object",
            "properties": {"candidate_id": {"type": "string"}},
            "required": ["candidate_id"],
        },
    )

    def run(self, candidate_id: str) -> str:
        candidate = self.repository.get_candidate(candidate_id)
        if candidate.team_run_id != self.team_run_id:
            return "Error: Candidate does not belong to this TeamRun"
        binding = self.repository.get_attempt_worktree_binding(candidate.attempt_id)
        if binding is None:
            return "Error: Candidate Worktree binding is missing"
        binding = self.worktrees.validate_binding(binding.id)
        snapshot_matches = self.worktrees.candidate_snapshot_matches(
            binding.id,
            expected_hash=candidate.diff_hash,
            expected_head=candidate.worktree_head,
        )
        from codeagent.legacy.teams.policy import collaborative
        if not snapshot_matches and not collaborative(self.repository.get_team_run(self.team_run_id).metadata):
            return "Error: Candidate changed after submission"
        artifact = Path(candidate.diff_ref).resolve()
        manager = (
            self.worktrees.for_team(self.team_run_id)
            if hasattr(self.worktrees, "for_team")
            else self.worktrees
        )
        managed_root = Path(manager.managed_root).resolve()
        try:
            artifact.relative_to(managed_root)
        except ValueError:
            return "Error: Candidate patch is outside Runtime-managed data"
        if artifact.name != f"{candidate.id}.patch" or not artifact.is_file():
            return "Error: Candidate patch reference is invalid"
        payload = candidate.to_dict()
        payload["snapshot_matches"] = snapshot_matches
        if not snapshot_matches:
            payload["required_action"] = "工作目录已变化；下面是原始成果快照，仅供诊断。不可接受为当前成果，安排返工后重新提交。"
        payload["patch"] = artifact.read_text(encoding="utf-8")
        payload["validation_runs"] = []
        for validation in self.repository.list_validation_runs(candidate.id):
            record = validation.to_dict()
            if validation.output_ref:
                log = Path(validation.output_ref).resolve()
                log.relative_to(managed_root)
                record["output_excerpt"] = log.read_text(encoding="utf-8")[-8000:] if log.is_file() else "验证日志不可用"
            payload["validation_runs"].append(record)
        return json.dumps(payload, ensure_ascii=False, indent=2)


@dataclass(slots=True)
class TeamAnswerQuestionTool:
    repository: Any
    team_run_id: str
    lead_agent_id: str
    definition: ToolDefinition = ToolDefinition(
        name="team_answer_question",
        description=(
            "Answer one Teammate QUESTION. Set scope_changed=true only to report that "
            "the user's hard authorization or task objective must change; the Attempt will remain paused. "
            "In V2, coordinating necessary edits outside the planned responsibility is not by itself a scope change. Keep user restrictions intact."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "answer": {"type": "string"},
                "scope_changed": {"type": "boolean"},
            },
            "required": ["message_id", "answer", "scope_changed"],
        },
    )

    def run(self, message_id: str, answer: str, scope_changed: bool) -> str:
        message = self.repository.answer_team_question(
            self.team_run_id,
            question_id=message_id, lead_agent_id=self.lead_agent_id,
            answer=answer, scope_changed=scope_changed,
        )
        if scope_changed:
            return (
                f"Answer persisted: {message.id}. Attempt remains paused because a "
                "new user-approved Team Plan is required."
            )
        return f"Answer persisted: {message.id}. Runtime resumes only the matching question wait at a safe boundary."


@dataclass(slots=True)
class TeamAttemptPlanDecisionTool:
    repository: Any
    worktrees: WorktreeManager
    team_run_id: str
    lead_agent_id: str
    definition: ToolDefinition = ToolDefinition(
        name="team_decide_attempt_plan",
        description="Approve or reject one submitted Attempt Plan as the Team Lead.",
        input_schema={
            "type": "object",
            "properties": {
                "attempt_id": {"type": "string"},
                "revision": {"type": "integer", "minimum": 1},
                "decision": {"type": "string", "enum": ["approve", "reject"]},
                "reason": {"type": "string"},
            },
            "required": ["attempt_id", "revision", "decision", "reason"],
        },
    )

    def run(self, attempt_id: str, revision: int, decision: str, reason: str) -> str:
        attempt = self.repository.get_task_attempt(attempt_id)
        if attempt.team_run_id != self.team_run_id:
            return "Error: Attempt does not belong to this TeamRun"
        fingerprint = None
        if decision == "approve":
            binding = self.repository.get_attempt_worktree_binding(attempt.id)
            if binding is None:
                return "Error: Code Attempt has no Worktree binding"
            fingerprint = self.worktrees.validate_binding(binding.id).fingerprint
        plan = self.repository.decide_attempt_plan(
            attempt.id,
            int(revision),
            decision=decision,
            decided_by=self.lead_agent_id,
            reason=reason,
            command_id=f"lead-attempt-plan:{attempt.id}:{revision}:{decision}",
            validated_worktree_fingerprint=fingerprint,
        )
        return json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)


@dataclass(slots=True)
class TeamCandidateReviewTool:
    repository: Any
    worktrees: WorktreeManager
    team_run_id: str
    lead_agent_id: str
    definition: ToolDefinition = ToolDefinition(
        name="team_review_candidate",
        description=(
            "Accept or return one frozen Candidate after semantic review. Runtime "
            "performs validation and candidate commit separately."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["accept", "rework"]},
                "reason": {"type": "string"},
            },
            "required": ["candidate_id", "decision", "reason"],
        },
    )

    def run(self, candidate_id: str, decision: str, reason: str) -> str:
        candidate = self.repository.get_candidate(candidate_id)
        if candidate.team_run_id != self.team_run_id:
            return "Error: Candidate does not belong to this TeamRun"
        reviewed = CandidateService(self.repository, self.worktrees).review(
            candidate.id,
            decision=decision,
            reviewed_by=self.lead_agent_id,
            reason=reason,
            command_id=f"lead-candidate:{candidate.id}:{candidate.revision}:{decision}",
        )
        if decision == "accept" and reviewed.user_approval_required:
            return (
                f"Candidate {reviewed.id} accepted by Lead; explicit manual policy approval "
                "is required before Runtime validation."
            )
        return json.dumps(reviewed.to_dict(), ensure_ascii=False, indent=2)


@dataclass(slots=True)
class TeamIntegrationTool:
    repository: Any
    worktrees: Any
    team_run_id: str
    lead_agent_id: str
    definition: ToolDefinition = ToolDefinition(
        name="team_resolve_integration",
        description="Inspect a failed Team integration and its logs; request retry or a replacement same-scope code Attempt. Never edits user files or expands the approved plan.",
        input_schema={"type": "object", "properties": {
            "integration_id": {"type": "string"},
            "action": {"type": "string", "enum": ["inspect", "retry", "repair"]},
            "reason": {"type": "string"}}, "required": ["integration_id", "action"]},
    )

    def run(self, integration_id: str, action: str, reason: str = "") -> str:
        op = integration_summary(self.repository.get_team_integration(integration_id))
        if op["team_run_id"] != self.team_run_id:
            return "Error: Integration does not belong to this Team"
        if action == "inspect":
            manager = self.worktrees.for_team(self.team_run_id) if hasattr(self.worktrees, "for_team") else self.worktrees
            for validation in op["validations"]:
                path = Path(validation["output_ref"]).resolve()
                path.relative_to(manager.managed_root.resolve())
                validation["output"] = path.read_text(encoding="utf-8")[-16000:]
            return json.dumps(op, ensure_ascii=False)
        if action not in {"retry", "repair"}:
            return "Error: Unsupported integration action"
        # Local delivery conflicts involve concurrent user edits, not a failed
        # Candidate. A Lead cannot silently discard or change those edits.
        if op["kind"] == "delivery" and op["status"] == "conflicted":
            return "Error: Explain the local delivery conflict to the user; preserve both versions."
        result = self.repository.retry_team_integration(integration_id,
            actor=self.lead_agent_id, reason=reason, repair=action == "repair")
        return json.dumps(result, ensure_ascii=False)


@dataclass(slots=True)
class TeamRevisePlanTool:
    repository: Any
    team_run_id: str
    lead_agent_id: str
    definition: ToolDefinition = ToolDefinition(
        name="team_revise_plan",
        description="Propose a complete replacement Team plan for user confirmation. Preserve existing task IDs and contracts. Add explicit compensating tasks for completed work; never silently remove integrated code. To resolve a plan-change question while preserving the stopped member's original contract, explicitly include its ID in continue_attempts and explain the resolution in shared_context. Approval removes only the planning block; Runtime checks the retained work before continuation. Pending proposals do not stop compatible work.",
        input_schema={"type": "object", "properties": {"plan": {"type": "object"}}, "required": ["plan"]},
    )

    def run(self, plan: dict) -> str:
        team = self.repository.get_team_run(self.team_run_id)
        payload = {**plan, "base_commit": team.base_commit, "integration_mode": team.integration_mode, "kernel_version": 2}
        payload.pop("preview_plan_id", None)
        self.repository.validate_team_plan_tasks(team.task_list_id, payload, require_validation_commands=team.integration_mode == "managed")
        revision = self.repository.create_team_plan_revision(team.id, plan=payload, created_by=self.lead_agent_id, command_id="revision:" + uuid4().hex)
        result = self.repository.submit_team_plan_revision(team.id, revision.revision, command_id=f"submit-revision:{revision.revision}")
        return json.dumps(result.to_dict(), ensure_ascii=False)


@dataclass(slots=True)
class TeamResumeWorkTool:
    repository: Any
    worktrees: Any
    team_run_id: str
    lead_agent_id: str
    definition: ToolDefinition = ToolDefinition(
        name="team_resume_work",
        description="Give a concrete correction instruction to a safely stopped member that has not completed its submission. Bounded per task; cannot bypass unknown writes, permissions, or user cancellation.",
        input_schema={"type": "object", "properties": {
            "attempt_id": {"type": "string"}, "instruction": {"type": "string"}},
            "required": ["attempt_id", "instruction"]},
    )

    def run(self, attempt_id: str, instruction: str) -> str:
        attempt = self.repository.get_task_attempt(attempt_id)
        if attempt.team_run_id != self.team_run_id or not instruction.strip():
            raise ValueError("Matching Team and a concrete correction instruction are required")
        binding = self.repository.get_attempt_worktree_binding(attempt_id)
        fingerprint = None
        if binding:
            manager = self.worktrees.for_team(self.team_run_id) if hasattr(self.worktrees, "for_team") else self.worktrees
            binding = manager.validate_recoverable_binding(binding.id)
            from codeagent.legacy.teams.policy import authorized_path
            team = self.repository.get_team_run(self.team_run_id)
            contract = self.repository.get_attempt_contract(attempt_id)
            if any(not authorized_path(p, team.metadata, contract, binding.write_scopes)
                   for p in manager.recovery_changed_paths(binding.id)):
                raise ValueError("Retained changes cross the user authorization boundary")
            fingerprint = binding.fingerprint
        result = self.repository.resume_task_attempt(attempt_id, resumed_by=self.lead_agent_id,
            reason=instruction, command_id=f"lead-correction:{attempt_id}:{attempt.updated_at}",
            validated_worktree_fingerprint=fingerprint)
        return json.dumps(result.to_dict(), ensure_ascii=False)


@dataclass(slots=True)
class TeamWaitTool:
    yield_callback: Callable[[str], None]
    definition: ToolDefinition = ToolDefinition(
        name="team_wait",
        description="Pause the Lead Session until a new user or Team event arrives.",
        input_schema={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    )

    def run(self, reason: str) -> str:
        self.yield_callback(reason)
        return "Lead Session will pause at the next safe boundary."


def create_teammate_tools(
    repository: Any,
    worktrees: WorktreeManager | None,
    attempt: TaskAttemptRecord,
    *,
    task_kind: str = "code",
    write_enabled: bool = False,
    yield_callback: Callable[[str], None],
) -> list[Any]:
    common = [
        TeamProgressTool(repository, attempt),
        TeamQuestionTool(repository, attempt, yield_callback),
    ]
    if str(task_kind).lower() == "analysis":
        return [SubmitAnalysisResultTool(repository, attempt), *common]
    if worktrees is None:
        raise ValueError("Code Teammate tools require a Worktree manager")
    work_tool = (
        SubmitCandidateTool(
            CandidateService(repository, worktrees), attempt, yield_callback
        )
        if write_enabled
        else SubmitAttemptPlanTool(repository, worktrees, attempt, yield_callback)
    )
    return [work_tool, *common]


def create_lead_tools(
    repository: Any,
    worktrees: WorktreeManager,
    team_run_id: str,
    lead_agent_id: str,
    *,
    allow_code: bool,
    yield_callback: Callable[[str], None],
) -> list[Any]:
    from codeagent.legacy.teams.policy import collaborative
    coordination = [TeamResumeWorkTool(repository, worktrees, team_run_id, lead_agent_id),
                    TeamRevisePlanTool(repository, team_run_id, lead_agent_id)] if collaborative(repository.get_team_run(team_run_id).metadata) else []
    return [
        *coordination,
        TeamStatusTool(repository, team_run_id, allow_code),
        TeamCandidateContextTool(repository, worktrees, team_run_id),
        TeamAnswerQuestionTool(repository, team_run_id, lead_agent_id),
        TeamAttemptPlanDecisionTool(
            repository, worktrees, team_run_id, lead_agent_id
        ),
        TeamCandidateReviewTool(repository, worktrees, team_run_id, lead_agent_id),
        TeamIntegrationTool(repository, worktrees, team_run_id, lead_agent_id),
        TeamWaitTool(yield_callback),
    ]


def _active_lead_generation(repository: Any, team: Any) -> int:
    sessions = repository.list_agent_sessions(team.id, role="lead")
    active = [
        session
        for session in sessions
        if session.state.value not in {"lost", "failed", "shutdown"}
    ]
    if len(active) != 1:
        raise RuntimeError("TeamRun must have exactly one active Lead Session")
    return active[0].generation


__all__ = [
    "SubmitAttemptPlanTool",
    "SubmitAnalysisResultTool",
    "SubmitCandidateTool",
    "TeamProgressTool",
    "TeamQuestionTool",
    "TeamStatusTool",
    "create_teammate_tools",
    "create_lead_tools",
]
