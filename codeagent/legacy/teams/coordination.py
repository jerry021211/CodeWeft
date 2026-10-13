"""Durable contracts and a read model for Team coordination.

Issues describe existing workflow records; resolving an issue never grants
permission or changes an Attempt. Only the underlying domain operation can do so.
"""
from __future__ import annotations

import json

from codeagent.events import utc_now_iso

COORDINATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_attempt_contracts (
    attempt_id TEXT PRIMARY KEY REFERENCES task_attempts(id),
    contract_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_contract_approvals (
    team_run_id TEXT NOT NULL, revision INTEGER NOT NULL, attempt_id TEXT NOT NULL,
    PRIMARY KEY(team_run_id, revision, attempt_id)
);
CREATE TABLE IF NOT EXISTS team_coordination_issues (
    id TEXT PRIMARY KEY, team_run_id TEXT NOT NULL REFERENCES team_runs(id),
    subject_id TEXT NOT NULL, reason TEXT NOT NULL, owner TEXT NOT NULL,
    status TEXT NOT NULL, evidence_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_team_issues ON team_coordination_issues(team_run_id, status);
"""

# Ownership is not a recovery permission. Unknown causes are deliberately visible.
REASONS = {
    "scope_violation": ("user", "修改触及授权边界，已保留工作现场", "核对具体文件后再决定是否继续"),
    "unknown_write_result": ("user", "执行中断，无法确认上一次写操作的结果", "检查现场；系统不会自动重放写操作"),
    "service_restart": ("user", "服务重启中断了任务，工作文件仍然保留", "点击重新检查并继续"),
    "protocol_incomplete": ("lead", "成员已结束本轮，但尚未提交完整成果", "Lead 需要检查最后一次提交结果并安排补交"),
    "team_plan_change_required": ("user", "任务需要调整已批准的方案", "查看成员的问题，确认新的任务安排"),
    "agent_iteration_limit": ("user", "本轮执行达到次数上限", "检查已有进度后决定是否继续"),
    "model_response_timeout": ("user", "模型响应超时，执行已经停止", "检查模型连接后继续"),
    "model_call_timeout": ("user", "模型调用超时，执行已经停止", "检查模型连接后继续"),
    "agent_runtime_failed": ("user", "成员运行异常，工作现场已保留", "查看错误详情后继续"),
    "dispatch_failed": ("runtime", "任务无法启动", "查看环境或工作目录错误"),
    "validation_failed": ("lead", "成果验证未通过，正在等待修复安排", "Lead 查看验证日志，区分环境问题和代码问题，再安排修复"),
    "user_candidate_approval": ("user", "该团队启用了人工成果确认", "核对成果内容后确认或退回；其他成果不受这次决定影响"),
}


class TeamCoordinationStorage:
    @staticmethod
    def _automatic_budget_exhausted(policy):
        return sum(policy.get("repair_counts", {}).values()) + sum(policy.get("coordination_resume_counts", {}).values()) >= 8

    def record_dispatch_issue(self, team_id, task_id, error=None):
        """An unsuccessful dispatch must be visible even before an Attempt exists."""
        now = utc_now_iso()
        identifier = f"dispatch:{team_id}:{task_id}"
        with self._transaction(immediate=True) as db:
            team = self._team_row(db, team_id)
            if json.loads(team["metadata_json"]).get("kernel_version") != 2:
                return
            old = db.execute("SELECT status,evidence_json FROM team_coordination_issues WHERE id=?", (identifier,)).fetchone()
            if error is None:
                if not old or old["status"] == "resolved":
                    return
                db.execute("UPDATE team_coordination_issues SET status='resolved',version=version+1,updated_at=? WHERE id=?", (now, identifier))
            else:
                evidence = json.dumps({"task_id": task_id, "summary": "任务尚未启动：工作环境检查或任务分配失败",
                    "next_step": "查看错误详情，修复环境后系统会重新检查；尚未开始修改源码", "error": str(error)}, ensure_ascii=False)
                if old and old["status"] == "open" and old["evidence_json"] == evidence:
                    return
                db.execute("""INSERT INTO team_coordination_issues
                    (id,team_run_id,subject_id,reason,owner,status,evidence_json,created_at,updated_at)
                    VALUES (?,?,?,'dispatch_failed','user','open',?,?,?) ON CONFLICT(id) DO UPDATE SET
                    status='open',evidence_json=excluded.evidence_json,updated_at=excluded.updated_at,version=team_coordination_issues.version+1""",
                    (identifier, team_id, task_id, evidence, now, now))
            self._append_team_event(db, team, "team.coordination.updated", {"issue_id": identifier, "task_id": task_id})
        self._notify_activity()

    @staticmethod
    def _consume_repair_budget(db, team, task_id):
        from codeagent.web.storage import StorageConflictError
        policy = json.loads(team["metadata_json"])
        if policy.get("kernel_version") != 2:
            return
        counts = policy.setdefault("repair_counts", {})
        key = str(task_id)
        if counts.get(key, 0) >= 2 or TeamCoordinationStorage._automatic_budget_exhausted(policy):
            raise StorageConflictError("该任务已达到自动修复次数上限，请向用户报告已尝试的修复与剩余问题")
        counts[key] = counts.get(key, 0) + 1
        db.execute("UPDATE team_runs SET metadata_json=? WHERE id=?", (json.dumps(policy), team["id"]))

    def defer_candidate_validation(self, candidate_id):
        """Called only after the validator confirms its child process has stopped."""
        from codeagent.web.storage import StorageConflictError
        with self._transaction(immediate=True) as db:
            candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            team = self._team_row(db, candidate["team_run_id"])
            if team["state"] not in {"running", "pausing", "paused", "cancelled"} or candidate["status"] != "validating":
                raise StorageConflictError("Validation is not at a resumable pause boundary")
            db.execute("UPDATE candidates SET status='accepted' WHERE id=?", (candidate_id,))
            db.execute("UPDATE task_attempts SET state='candidate_submitted',write_enabled=0 WHERE id=?", (candidate["attempt_id"],))
            self._append_team_event(db, team, "team.candidate.validation_deferred", {"candidate_id": candidate_id})
        self._notify_activity()

    @staticmethod
    def _attempt_plan_compatible(db, team, attempt):
        if team["active_plan_revision"] == attempt["team_plan_revision"]:
            return True
        if json.loads(team["metadata_json"]).get("kernel_version") != 2:
            return False
        return db.execute("SELECT 1 FROM team_contract_approvals WHERE team_run_id=? AND revision=? AND attempt_id=?",
                          (team["id"], team["active_plan_revision"], attempt["id"])).fetchone() is not None

    @staticmethod
    def _approve_compatible_contracts(db, team, revision, plan):
        """Additive revisions preserve old immutable Attempts, never reinterpret them.

        Replacing completed/in-flight work requires an explicit compensating task;
        deleting its ID from a plan must not make its code disappear logically.
        """
        from codeagent.web.storage import StorageConflictError
        planned = {str(t["task_id"]) for t in plan.get("tasks", []) if isinstance(t, dict) and t.get("task_id")}
        for row in db.execute("SELECT a.*, c.contract_json FROM task_attempts a JOIN team_attempt_contracts c ON c.attempt_id=a.id WHERE a.team_run_id=? AND a.state NOT IN ('failed','cancelled','orphaned')", (team["id"],)).fetchall():
            current = db.execute("SELECT metadata_json FROM tasks WHERE task_list_id=? AND id=?", (row["task_list_id"], row["task_id"])).fetchone()
            if row["task_id"] not in planned or not current or json.loads(current[0]) != json.loads(row["contract_json"]):
                raise StorageConflictError("已有任务的执行约定不能被静默替换；请保留原任务，并新增明确的修正任务与依赖")
            db.execute("INSERT OR IGNORE INTO team_contract_approvals VALUES (?,?,?)", (team["id"], revision, row["id"]))
        # A user may explicitly resolve a planning question while keeping the old
        # contract. This only removes the planning block; filesystem validation
        # and generation checks still run before the member can write again.
        continuation = plan.get("continue_attempts", [])
        if not isinstance(continuation, list) or any(not isinstance(a, str) for a in continuation):
            raise StorageConflictError("continue_attempts 必须是明确列出的任务执行 ID")
        for attempt_id in continuation:
            attempt = db.execute("SELECT * FROM task_attempts WHERE id=? AND team_run_id=?", (attempt_id, team["id"])).fetchone()
            if (not attempt or attempt["state"] != "waiting" or attempt["result_unknown"]
                    or attempt["cancel_requested_at"] or not attempt["worker_exited_at"]
                    or json.loads(attempt["error_json"] or "{}").get("type") != "team_plan_change_required"
                    or not db.execute("SELECT 1 FROM team_contract_approvals WHERE team_run_id=? AND revision=? AND attempt_id=?", (team["id"], revision, attempt_id)).fetchone()):
                raise StorageConflictError("只能在确认成员已停止且原执行约定仍有效后解除方案等待；请先核对现场")
            now = utc_now_iso()
            db.execute("UPDATE task_attempts SET error_json=?,updated_at=? WHERE id=?",
                (json.dumps({"type": "protocol_incomplete", "approved_continuation_revision": revision}), now, attempt_id))
            db.execute("UPDATE agent_sessions SET waiting_reason='protocol_incomplete',updated_at=? WHERE id=?", (now, attempt["session_id"]))

    def get_attempt_contract(self, attempt_id):
        with self._lock:
            row = self._connection.execute("SELECT contract_json FROM team_attempt_contracts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row:
                return json.loads(row[0])
        attempt = self.get_task_attempt(attempt_id)
        # Compatibility only: never retroactively assign V2 rights to old Attempts.
        return dict(self.get_task_resource(attempt.task_list_id, attempt.task_id).task.metadata)

    @classmethod
    def _sync_coordination(cls, db, team_id):
        """Called inside the domain event transaction; no independent state machine."""
        now = utc_now_iso()
        active = set()
        team = cls._team_row(db, team_id)
        policy = json.loads(team["metadata_json"])
        for row in db.execute("SELECT a.*, s.waiting_reason FROM task_attempts a LEFT JOIN agent_sessions s ON s.id=a.session_id WHERE a.team_run_id=? AND a.state IN ('waiting','orphaned','validation_failed')", (team_id,)).fetchall():
            error = json.loads(row["error_json"] or "{}")
            reason = "unknown_write_result" if row["result_unknown"] else error.get("type") or row["waiting_reason"]
            if not row["result_unknown"] and row["state"] == "validation_failed":
                reason = "validation_failed"
            if not reason or reason in {"team_paused", "candidate_review", "runtime_validation"} or reason.startswith("waiting_for_lead_answer:"):
                continue
            owner, summary, next_step = REASONS.get(reason, ("lead", "任务等待协调：" + reason, "Lead 需要检查成员状态并安排下一步"))
            if reason == "protocol_incomplete" and (policy.get("kernel_version") != 2 or policy.get("coordination_resume_counts", {}).get(str(row["task_id"]), 0) >= 2 or cls._automatic_budget_exhausted(policy)):
                owner, next_step = "user", "自动补交未能完成，请查看最后一次失败记录后决定是否继续"
            if reason == "validation_failed" and (policy.get("repair_counts", {}).get(str(row["task_id"]), 0) >= 2 or cls._automatic_budget_exhausted(policy)):
                owner, next_step = "user", "自动修复次数已用尽；查看修复和验证记录，调整方案后继续"
            identifier = row["id"] + ":" + reason
            active.add(identifier)
            existed = db.execute("SELECT 1 FROM team_coordination_issues WHERE id=?", (identifier,)).fetchone()
            evidence = json.dumps({"task_id": row["task_id"], "attempt_id": row["id"], "summary": summary,
                "next_step": next_step, "error": error, "result_unknown": bool(row["result_unknown"])}, ensure_ascii=False, sort_keys=True)
            db.execute("""INSERT INTO team_coordination_issues
                (id,team_run_id,subject_id,reason,owner,status,evidence_json,created_at,updated_at)
                VALUES (?,?,?,?,?,'open',?,?,?) ON CONFLICT(id) DO UPDATE SET
                status='open',owner=excluded.owner,evidence_json=excluded.evidence_json,
                version=team_coordination_issues.version+1,updated_at=excluded.updated_at
                WHERE team_coordination_issues.status!='open' OR team_coordination_issues.evidence_json!=excluded.evidence_json""",
                (identifier, team_id, row["id"], reason, owner, evidence, now, now))
            if owner == "lead" and not existed:
                team = cls._team_row(db, team_id)
                session = db.execute("SELECT generation FROM agent_sessions WHERE agent_id=? AND state NOT IN ('lost','failed','shutdown')", (team["lead_agent_id"],)).fetchone()
                if session:
                    cls._insert_team_message(db, team=team, sender_type="runtime", recipient_type="lead",
                        recipient_agent_id=team["lead_agent_id"], recipient_generation=session[0],
                        message_type="SYSTEM_ERROR", payload={"reason_code": reason, "reason": summary,
                            "next_action": "Inspect the task and last failed submission. For a safely stopped incomplete submission, use team_resume_work with a concrete correction instruction. Do not replay unknown writes."},
                        dedupe_key="coordination:" + identifier, task_id=row["task_id"], attempt_id=row["id"],
                        created_at=now, priority="control")
        if policy.get("kernel_version") == 2:
            for op in db.execute("SELECT i.*,c.task_id FROM team_integrations i LEFT JOIN candidates c ON c.id=i.candidate_id WHERE i.team_run_id=? AND i.status IN ('validation_failed','conflicted','interrupted','recovery_required')", (team_id,)).fetchall():
                task_id = op["task_id"] or "最终验收"
                exhausted = policy.get("repair_counts", {}).get(str(op["task_id"] or "delivery"), 0) >= 2 or cls._automatic_budget_exhausted(policy)
                owner = "user" if exhausted or op["status"] == "recovery_required" or (op["kind"] == "delivery" and op["status"] == "conflicted") else "lead"
                identifier = "integration:" + op["id"]
                active.add(identifier)
                evidence = json.dumps({"task_id": task_id, "operation_id": op["id"],
                    "summary": "集成或最终验收尚未通过",
                    "next_step": "自动修复预算已用尽；查看已尝试的修复与日志后决定下一步" if exhausted else
                        "核对本地冲突或未确认的执行现场，保留双方修改" if owner == "user" else "Lead 检查具体失败日志，安排修复或处理环境后再验证",
                    "error": op["error"]}, ensure_ascii=False, sort_keys=True)
                db.execute("""INSERT INTO team_coordination_issues
                    (id,team_run_id,subject_id,reason,owner,status,evidence_json,created_at,updated_at)
                    VALUES (?,?,?,'integration_blocked',?,'open',?,?,?) ON CONFLICT(id) DO UPDATE SET
                    status='open',owner=excluded.owner,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at,version=team_coordination_issues.version+1
                    WHERE team_coordination_issues.status!='open' OR team_coordination_issues.evidence_json!=excluded.evidence_json OR team_coordination_issues.owner!=excluded.owner""",
                    (identifier, team_id, op["id"], owner, evidence, now, now))
        for row in db.execute("SELECT id FROM team_coordination_issues WHERE team_run_id=? AND status='open'", (team_id,)).fetchall():
            if row["id"] not in active and not row["id"].startswith("dispatch:"):
                db.execute("UPDATE team_coordination_issues SET status='resolved',version=version+1,updated_at=? WHERE id=?", (now, row["id"]))

    def list_coordination_issues(self, team_id):
        with self._lock:
            rows = self._connection.execute("SELECT * FROM team_coordination_issues WHERE team_run_id=? ORDER BY created_at,id", (team_id,)).fetchall()
        return [{k: v for k, v in {**dict(row), "evidence": json.loads(row["evidence_json"])}.items() if k != "evidence_json"} for row in rows]
