"""Candidate freezing, Lead review checks, Runtime validation and commit."""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from codeagent.runtime.validation import ValidationRunner, validate_step, command_label
from codeagent.legacy.teams.models import CandidateRecord, CandidateStatus
from codeagent.legacy.teams.worktrees import WorktreeError, WorktreeManager


class CandidateService(ValidationRunner):
    """Orchestrate Git side effects around transactional Candidate state."""

    def __init__(
        self,
        repository: Any,
        worktrees: WorktreeManager,
        *,
        validation_timeout: int = 600,
        stop_event=None,
        permission_check=None,
    ) -> None:
        super().__init__(timeout=validation_timeout)
        self.repository = repository
        self.worktrees = worktrees
        self.validation_team_id = None
        self.stop_event = stop_event
        self.permission_check = permission_check
        self.validation_attempt_id = None

    def submit(
        self,
        attempt_id: str,
        *,
        summary: str,
        tests_reported: Sequence[str] = (),
        known_risks: Sequence[str] = (),
        command_id: str | None = None,
    ) -> CandidateRecord:
        attempt = self.repository.get_task_attempt(attempt_id)
        binding = self.repository.get_attempt_worktree_binding(attempt_id)
        if binding is None:
            raise WorktreeError("Candidate Attempt has no Worktree binding")
        binding = self.worktrees.validate_binding(binding.id)
        candidate_id = f"candidate_{uuid4().hex}"
        snapshot = self.worktrees.capture_candidate(
            binding.id, candidate_id=candidate_id
        )
        from codeagent.legacy.teams.policy import collaborative
        from codeagent.worktrees.scopes import path_is_allowed
        team = self.repository.get_team_run(attempt.team_run_id)
        contract = self.repository.get_attempt_contract(attempt.id)
        if collaborative(team.metadata):
            extra = [p for p in (*snapshot.changed_files, *snapshot.untracked_files)
                     if not path_is_allowed(p, contract.get("write_scopes", []))]
            if extra:
                known_risks = [*known_risks, "Responsibility changes for Lead review: " + ", ".join(extra)]
        return self.repository.submit_candidate(
            attempt_id,
            candidate_id=candidate_id,
            summary=summary,
            changed_files=snapshot.changed_files,
            untracked_files=snapshot.untracked_files,
            diff_ref=snapshot.diff_ref,
            diff_hash=snapshot.diff_hash,
            base_commit=binding.base_commit,
            worktree_head=snapshot.head_commit,
            tests_reported=tests_reported,
            known_risks=known_risks,
            submitted_by=attempt.agent_id,
            command_id=command_id or f"submit-candidate:{candidate_id}",
        )

    def review(
        self,
        candidate_id: str,
        *,
        decision: str,
        reviewed_by: str,
        reason: str,
        command_id: str,
    ) -> CandidateRecord:
        candidate = self.repository.get_candidate(candidate_id)
        binding = self.repository.get_attempt_worktree_binding(candidate.attempt_id)
        if binding is None:
            raise WorktreeError("Candidate Worktree binding is missing")
        binding = self.worktrees.validate_binding(binding.id)
        if decision == "accept" and not self.worktrees.candidate_snapshot_matches(
            binding.id, expected_hash=candidate.diff_hash, expected_head=candidate.worktree_head,
        ):
            raise WorktreeError("Candidate changed after submission")
        return self.repository.decide_candidate_review(
            candidate_id,
            decision=decision,
            reviewed_by=reviewed_by,
            reason=reason,
            validated_diff_hash=candidate.diff_hash,
            command_id=command_id,
        )

    def validate_and_commit(
        self,
        candidate_id: str,
        *,
        trace_id: str | None = None,
    ) -> CandidateRecord:
        candidate = self.repository.get_candidate(candidate_id)
        if candidate.status is not CandidateStatus.ACCEPTED:
            raise ValueError("Lead must accept Candidate before Runtime validation")
        if (
            candidate.user_approval_required
            and candidate.user_decision != "approved"
        ):
            raise ValueError(
                "High-risk Candidate requires user approval before Runtime validation"
            )
        attempt = self.repository.get_task_attempt(candidate.attempt_id)
        binding = self.repository.get_attempt_worktree_binding(attempt.id)
        if binding is None:
            raise WorktreeError("Candidate Worktree binding is missing")
        binding = self.worktrees.validate_binding(binding.id)
        self._require_frozen_snapshot(candidate, binding.id)
        self.validation_team_id = attempt.team_run_id
        self.validation_attempt_id = attempt.id
        candidate = self.repository.mark_candidate_validating(candidate_id)
        team = self.repository.get_team_run(attempt.team_run_id)
        assert team is not None
        commands = ["git diff --check HEAD"]
        commands.extend(_string_list(team.metadata.get("mandatory_validation_commands")))
        commands.extend(_string_list(self.repository.get_attempt_contract(attempt.id).get("validation_commands")))
        commands = list(dict.fromkeys(commands))
        last_validation_id = ""
        for command in commands:
            validation = self.repository.begin_validation_run(
                candidate_id, command=command
            )
            last_validation_id = validation.id
            status, exit_code, output, duration_ms = self._run_validation(
                Path(binding.path), command
            )
            output_ref = self.worktrees.write_artifact(
                team.id, f"{validation.id}.log", output
            )
            self.repository.finish_validation_run(
                validation.id,
                status=status,
                exit_code=exit_code,
                output_ref=output_ref,
                duration_ms=duration_ms,
            )
            if self.validation_cleanup_unknown:
                self.repository.mark_candidate_commit_unknown(candidate_id,
                    reason="验证进程未能确认停止，需核对进程与工作目录后恢复；不能自动重跑。")
                return self.repository.get_candidate(candidate_id)
            if status != "passed" and self.validation_interrupted:
                self.repository.defer_candidate_validation(candidate_id)
                return self.repository.get_candidate(candidate_id)
            if status != "passed":
                return self.repository.mark_candidate_validation_failed(
                    candidate_id,
                    summary=f"Validation failed: {command}",
                    validation_run_id=validation.id,
                )
            if not self.worktrees.candidate_snapshot_matches(
                binding.id,
                expected_hash=candidate.diff_hash,
                expected_head=candidate.worktree_head,
            ):
                return self.repository.mark_candidate_validation_failed(
                    candidate_id,
                    summary=f"Validation changed the frozen Candidate: {command}",
                    validation_run_id=validation.id,
                )

        commit_validation = self.repository.begin_validation_run(
            candidate_id, command="runtime:candidate_commit"
        )
        last_validation_id = commit_validation.id
        started = time.monotonic()
        old_head = candidate.worktree_head
        try:
            commit = self.worktrees.runtime_commit_candidate(
                binding.id,
                expected_paths=tuple(
                    dict.fromkeys(
                        [*candidate.changed_files, *candidate.untracked_files]
                    )
                ),
                message=_commit_message(candidate, attempt, trace_id),
            )
        except BaseException as exc:
            duration_ms = round((time.monotonic() - started) * 1000)
            output_ref = self.worktrees.write_artifact(
                team.id,
                f"{commit_validation.id}.log",
                f"{type(exc).__name__}: {exc}",
            )
            self.repository.finish_validation_run(
                commit_validation.id,
                status="failed",
                exit_code=1,
                output_ref=output_ref,
                duration_ms=duration_ms,
            )
            if self.worktrees.current_head(binding.id) != old_head:
                self.repository.mark_candidate_commit_unknown(
                    candidate_id,
                    reason=f"Candidate commit outcome requires inspection: {exc}",
                )
                raise
            return self.repository.mark_candidate_validation_failed(
                candidate_id,
                summary=f"Runtime candidate commit failed: {exc}",
                validation_run_id=commit_validation.id,
            )
        duration_ms = round((time.monotonic() - started) * 1000)
        output_ref = self.worktrees.write_artifact(
            team.id,
            f"{commit_validation.id}.log",
            f"Created candidate commit {commit.commit_hash}\n",
        )
        self.repository.finish_validation_run(
            commit_validation.id,
            status="passed",
            exit_code=0,
            output_ref=output_ref,
            duration_ms=duration_ms,
        )
        try:
            return self.repository.complete_candidate_commit(
                candidate_id,
                commit_hash=commit.commit_hash,
                head_commit=commit.head_commit,
                worktree_fingerprint=commit.fingerprint,
                validation_run_id=last_validation_id,
            )
        except BaseException as exc:
            self.repository.mark_candidate_commit_unknown(
                candidate_id,
                reason=f"Commit created but persistence failed: {exc}",
            )
            raise

    def _require_frozen_snapshot(
        self, candidate: CandidateRecord, worktree_id: str
    ) -> None:
        if not self.worktrees.candidate_snapshot_matches(
            worktree_id,
            expected_hash=candidate.diff_hash,
            expected_head=candidate.worktree_head,
        ):
            raise WorktreeError("Candidate changed after review")

    def _run_validation(self, cwd, command):
        started = time.monotonic()
        try:
            self.validation_interrupted = False
            self.validation_cleanup_unknown = False
            validate_step(command)
            if self.validation_team_id:
                from codeagent.legacy.teams.policy import collaborative
                from codeagent.runtime.validation import ValidationCancellation
                from codeagent.runtime.cancellation import CancelledError
                if collaborative(self.repository.get_team_run(self.validation_team_id).metadata):
                    cancellation = ValidationCancellation(lambda: self._validation_stopped())
                    try:
                        cancellation.raise_if_cancelled()
                        if self.permission_check is not None:
                            decision = self.permission_check(self.validation_team_id, self.validation_attempt_id, cwd, command, cancellation)
                        else:
                            # Non-web embedders still use the ordinary permission
                            # policy; they must supply a broker to grant a request.
                            from codeagent.permissions import PermissionPolicy
                            from codeagent.runtime.validation import permission_input
                            decision = PermissionPolicy(workspace=cwd, ask=lambda *_: False, cancellation=cancellation).check("bash", permission_input(cwd, command))
                        cancellation.raise_if_cancelled()
                    except CancelledError:
                        self.validation_interrupted = True
                        return "failed", 130, "Validation approval interrupted; command not executed", round((time.monotonic() - started) * 1000)
                    if not decision.allowed:
                        return "failed", 126, "Validation command not executed: " + decision.reason, round((time.monotonic() - started) * 1000)
            return super().run(cwd, command)
        except (OSError, ValueError) as exc:
            return "failed", 1, f"{type(exc).__name__}: {exc}", round((time.monotonic() - started) * 1000)

    def _validation_stopped(self):
        return bool((self.validation_team_id and self.repository.get_team_run(self.validation_team_id).state.value != "running")
                    or (self.stop_event is not None and self.stop_event.is_set()))



def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("Validation commands must be a list")
    return [str(item) for item in value if str(item).strip()]


def _commit_message(candidate: CandidateRecord, attempt: Any, trace_id: str | None) -> str:
    subject = candidate.summary.strip().splitlines()[0][:72] or "Agent Team candidate"
    trailers = [
        f"Team-Run: {candidate.team_run_id}",
        f"Task: {candidate.task_id}",
        f"Attempt: {candidate.attempt_id}",
        f"Attempt-Ordinal: {attempt.ordinal}",
        f"Candidate: {candidate.id}",
    ]
    if trace_id:
        trailers.append(f"Trace: {trace_id}")
    return subject + "\n\n" + "\n".join(trailers)


__all__ = ["CandidateService"]
