"""Transactional journal for local Team integration and delivery.

Git and SQLite are reconciled through durable publish intent; filesystem work
never runs while a database transaction is held.
"""

from __future__ import annotations

import json

from codeagent.events import utc_now_iso


INTEGRATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_integrations (
    id TEXT PRIMARY KEY,
    team_run_id TEXT NOT NULL REFERENCES team_runs(id),
    kind TEXT NOT NULL,
    candidate_id TEXT REFERENCES candidates(id),
    candidate_commit TEXT,
    parent_head TEXT NOT NULL,
    parent_revision INTEGER NOT NULL,
    plan_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    trial_commit TEXT,
    worktree_path TEXT NOT NULL,
    commands_json TEXT NOT NULL DEFAULT '[]',
    validations_json TEXT NOT NULL DEFAULT '[]',
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS team_one_integration_writer
ON team_integrations(team_run_id)
WHERE status IN ('preparing','validating','publishing','applying');
CREATE UNIQUE INDEX IF NOT EXISTS team_candidate_published_once
ON team_integrations(candidate_id) WHERE status = 'published';
"""


def _record(row):
    if row is None:
        return None
    value = dict(row)
    for name in ("commands", "validations", "result"):
        value[name] = json.loads(value.pop(name + "_json"))
    return value


class TeamIntegrationStorage:
    def list_team_integrations(self, team_run_id):
        with self._lock:
            return [_record(row) for row in self._connection.execute(
                "SELECT * FROM team_integrations WHERE team_run_id=? ORDER BY created_at,id",
                (team_run_id,),
            ).fetchall()]

    def get_team_integration(self, identifier):
        with self._lock:
            row = self._connection.execute("SELECT * FROM team_integrations WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("Team integration does not exist")
            return _record(row)

    def begin_team_integration(self, team_run_id, *, identifier, kind, candidate_id,
                               worktree_path, commands):
        from codeagent.web.storage import StorageConflictError
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            team = self._team_row(connection, team_run_id)
            if team["integration_mode"] != "managed" or team["state"] != "running" or not team["active_plan_revision"]:
                raise StorageConflictError("Team is not running with managed integration")
            candidate = None
            if kind == "candidate":
                candidate = connection.execute(
                    "SELECT * FROM candidates WHERE id=? AND team_run_id=? AND status='committed' AND superseded_at IS NULL",
                    (candidate_id, team_run_id),
                ).fetchone()
                if candidate is None or candidate["team_integrated_revision"] is not None:
                    raise StorageConflictError("Candidate is not available for integration")
            elif kind == "delivery":
                if not self._managed_delivery_ready(connection, team):
                    raise StorageConflictError("Team still has unfinished or unintegrated work")
            else:
                raise ValueError("Unknown integration kind")
            connection.execute("""
                INSERT INTO team_integrations(id,team_run_id,kind,candidate_id,candidate_commit,
                    parent_head,parent_revision,plan_revision,status,worktree_path,commands_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,'preparing',?,?,?,?)
                """, (identifier, team_run_id, kind, candidate_id,
                      candidate["commit_hash"] if candidate else None,
                      team["integration_head"], team["integration_revision"], team["active_plan_revision"],
                      worktree_path, json.dumps(commands), now, now))
            self._append_team_event(connection, team, "team.integration.started", {
                "team_run_id": team_run_id, "integration_id": identifier, "kind": kind,
                "candidate_id": candidate_id, "parent_head": team["integration_head"],
            })
        return self.get_team_integration(identifier)

    def update_team_integration(self, identifier, *, status=None, trial_commit=None,
                                validations=None, result=None, error=None):
        from codeagent.web.storage import StorageConflictError
        with self._transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM team_integrations WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("Missing integration operation")
            if row["status"] in {"published", "delivered", "superseded"}:
                raise StorageConflictError("Integration operation is already final")
            values = {"updated_at": utc_now_iso()}
            for key, value in (("status", status), ("trial_commit", trial_commit), ("error", error)):
                if value is not None:
                    values[key] = value
            for key, value in (("validations_json", validations), ("result_json", result)):
                if value is not None:
                    values[key] = json.dumps(value, ensure_ascii=False)
            connection.execute("UPDATE team_integrations SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                               (*values.values(), identifier))
            team = self._team_row(connection, row["team_run_id"])
            self._append_team_event(connection, team, "team.integration.updated", {
                "team_run_id": team["id"], "integration_id": identifier,
                "status": status or row["status"], "error": error,
            })
            if status in {"conflicted", "validation_failed", "interrupted", "recovery_required"}:
                self._integration_message(connection, team, identifier, status, error or status)
        self._notify_activity()
        return self.get_team_integration(identifier)

    def publish_team_integration(self, identifier):
        from codeagent.web.storage import StorageConflictError
        now = utc_now_iso()
        with self._transaction(immediate=True) as connection:
            op = connection.execute("SELECT * FROM team_integrations WHERE id=?", (identifier,)).fetchone()
            if op["status"] == "published":
                return _record(op)
            team = self._team_row(connection, op["team_run_id"])
            self._check_integration_publish(team, op)
            if op["kind"] != "candidate" or op["status"] != "publishing" or not op["trial_commit"]:
                raise StorageConflictError("Integration has no validated publish intent")
            validations = json.loads(op["validations_json"])
            if len(validations) != len(json.loads(op["commands_json"])) or any(v["status"] != "passed" or v["commit"] != op["trial_commit"] for v in validations):
                raise StorageConflictError("Integration validation is incomplete")
            revision = int(team["integration_revision"]) + 1
            connection.execute("UPDATE team_runs SET integration_head=?,integration_revision=?,updated_at=? WHERE id=?",
                               (op["trial_commit"], revision, now, team["id"]))
            connection.execute("UPDATE candidates SET team_integrated_revision=? WHERE id=? AND superseded_at IS NULL",
                               (revision, op["candidate_id"]))
            connection.execute("UPDATE team_integrations SET status='published',updated_at=? WHERE id=?", (now, identifier))
            self._integration_message(connection, team, identifier, "published", f"Candidate published in Team integration version C{revision}")
            self._append_team_event(connection, team, "team.integration.published", {
                "team_run_id": team["id"], "integration_id": identifier,
                "integration_head": op["trial_commit"], "integration_revision": revision,
            })
        self._notify_activity()
        return self.get_team_integration(identifier)

    @staticmethod
    def _check_integration_publish(team, op):
        from codeagent.web.storage import StorageConflictError
        if (team["state"] != "running" or team["integration_head"] != op["parent_head"]
                or team["integration_revision"] != op["parent_revision"]
                or team["active_plan_revision"] != op["plan_revision"]):
            raise StorageConflictError("Team state or integration version changed during validation")

    def complete_team_delivery(self, identifier):
        from codeagent.web.storage import StorageConflictError
        with self._transaction(immediate=True) as connection:
            op = connection.execute("SELECT * FROM team_integrations WHERE id=?", (identifier,)).fetchone()
            if op["status"] == "delivered":
                return _record(op)
            team = self._team_row(connection, op["team_run_id"])
            self._check_integration_publish(team, op)
            if op["kind"] != "delivery" or op["status"] != "applying":
                raise StorageConflictError("Delivery has no writeback intent")
            validations = json.loads(op["validations_json"])
            if len(validations) != len(json.loads(op["commands_json"])) or any(v["status"] != "passed" or v["commit"] != op["trial_commit"] for v in validations):
                raise StorageConflictError("Delivery validation is incomplete")
            if not self._managed_delivery_ready(connection, team):
                raise StorageConflictError("Team is no longer ready to deliver")
            now = utc_now_iso()
            connection.execute("UPDATE team_integrations SET status='delivered',updated_at=? WHERE id=?", (now, identifier))
            connection.execute("UPDATE team_runs SET state='completed',updated_at=? WHERE id=?", (now, team["id"]))
            self._integration_message(connection, team, identifier, "delivered",
                "Team results are now unstaged changes in the user's local project. HEAD and index were preserved; no push was performed. The user can test locally before committing.")
            self._append_team_event(connection, team, "team.delivery.completed", {
                "team_run_id": team["id"], "integration_id": identifier, "integration_head": team["integration_head"],
                "delivery_mode": "unstaged_local_changes", "pushed": False,
            })
        self._notify_activity()
        return self.get_team_integration(identifier)

    def managed_delivery_ready(self, team_run_id):
        with self._lock:
            return self._managed_delivery_ready(self._connection, self._team_row(self._connection, team_run_id))

    @staticmethod
    def _managed_delivery_ready(connection, team):
        if team["integration_mode"] != "managed" or team["state"] != "running" or not team["active_plan_revision"]:
            return False
        plan = connection.execute("SELECT plan_json FROM team_plan_revisions WHERE team_run_id=? AND revision=?",
                                  (team["id"], team["active_plan_revision"])).fetchone()
        ids = [str(t["task_id"]) for t in json.loads(plan["plan_json"]).get("tasks", []) if isinstance(t, dict) and t.get("task_id")]
        if not ids:
            return False
        tasks = connection.execute("SELECT id,status FROM tasks WHERE task_list_id=?", (team["task_list_id"],)).fetchall()
        if any(t["status"] not in {"completed", "cancelled"} for t in tasks if t["id"] in ids):
            return False
        return connection.execute("SELECT 1 FROM candidates WHERE team_run_id=? AND status='committed' AND superseded_at IS NULL AND team_integrated_revision IS NULL LIMIT 1", (team["id"],)).fetchone() is None

    @staticmethod
    def _attempt_base_allowed(connection, team, attempt):
        if attempt["attempt_base_commit"] == team["base_commit"]:
            return attempt["base_integration_revision"] == 0
        if team["integration_mode"] != "managed":
            return False
        return connection.execute("SELECT 1 FROM team_integrations WHERE team_run_id=? AND status='published' AND trial_commit=? AND parent_revision+1=? LIMIT 1",
                                  (team["id"], attempt["attempt_base_commit"], attempt["base_integration_revision"])).fetchone() is not None

    @staticmethod
    def _dependency_reasons(connection, team, task_id):
        blockers = connection.execute("""SELECT d.dependency_requirement,d.blocker_id,t.status,t.metadata_json
            FROM task_dependencies d JOIN tasks t ON t.task_list_id=d.task_list_id AND t.id=d.blocker_id
            WHERE d.task_list_id=? AND d.blocked_id=?""", (team["task_list_id"], task_id)).fetchall()
        reasons = []
        for blocker in blockers:
            code = json.loads(blocker["metadata_json"]).get("kind", "analysis") == "code"
            integrated = blocker["dependency_requirement"] == "candidate_integrated" or (team["integration_mode"] == "managed" and code)
            if integrated:
                predicate = "team_integrated_revision IS NOT NULL AND superseded_at IS NULL" if team["integration_mode"] == "managed" else "integrated_at IS NOT NULL"
                found = connection.execute("SELECT 1 FROM candidates WHERE team_run_id=? AND task_id=? AND status='committed' AND " + predicate + " LIMIT 1", (team["id"], blocker["blocker_id"])).fetchone()
                if not found:
                    reasons.append("candidate_not_integrated")
            elif blocker["status"] != "completed":
                reasons.append("dependency_not_completed")
        return list(dict.fromkeys(reasons))

    def retry_team_integration(self, identifier, *, actor, reason, repair=False):
        """Lead may retry a stopped operation or request a new, same-scope Attempt."""
        from codeagent.web.storage import StorageConflictError
        with self._transaction(immediate=True) as connection:
            op = connection.execute("SELECT * FROM team_integrations WHERE id=?", (identifier,)).fetchone()
            if op is None:
                raise ValueError("Missing integration operation")
            team = self._team_row(connection, op["team_run_id"])
            if actor not in {"user", team["lead_agent_id"]} or team["state"] != "running":
                raise StorageConflictError("Only the active Lead or user can resolve integration")
            if op["status"] not in {"conflicted", "validation_failed", "interrupted"}:
                raise StorageConflictError("Operation requires inspection or is not retryable")
            if not reason.strip():
                raise ValueError("Resolution reason is required")
            if repair:
                # Diagnostics guide the Lead; they are not authorization gates.
                # A dependency error can still require a scoped manifest repair.
                if not op["candidate_id"]:
                    raise StorageConflictError("Local delivery conflicts need local resolution before retry")
                candidate = connection.execute("SELECT * FROM candidates WHERE id=?", (op["candidate_id"],)).fetchone()
                if candidate["team_integrated_revision"] is not None or candidate["superseded_at"]:
                    raise StorageConflictError("Candidate is no longer eligible for replacement")
                attempts = connection.execute("SELECT COUNT(*) FROM task_attempts WHERE team_run_id=? AND task_id=?",
                                              (team["id"], candidate["task_id"])).fetchone()[0]
                if attempts >= self.team_max_attempts_per_task:
                    raise StorageConflictError("Task Attempt retry limit reached; retain the candidate and resolve the task plan with the user")
                now = utc_now_iso()
                connection.execute("UPDATE candidates SET superseded_at=? WHERE id=?", (now, candidate["id"]))
                connection.execute("UPDATE tasks SET status='pending',owner=NULL,revision=revision+1,updated_at=? WHERE task_list_id=? AND id=?",
                                   (now, team["task_list_id"], candidate["task_id"]))
                connection.execute("UPDATE task_lists SET revision=revision+1,updated_at=? WHERE id=?", (now, team["task_list_id"]))
            connection.execute("UPDATE team_integrations SET status='superseded',error=?,updated_at=? WHERE id=?",
                               (reason, utc_now_iso(), identifier))
        self._notify_activity()
        return self.get_team_integration(identifier)

    def _integration_message(self, connection, team, identifier, status, summary):
        dedupe_key = f"integration:{identifier}:{status}"
        if connection.execute("SELECT 1 FROM team_messages WHERE team_run_id=? AND dedupe_key=?",
                              (team["id"], dedupe_key)).fetchone():
            return
        session = connection.execute("SELECT generation FROM agent_sessions WHERE agent_id=? AND state NOT IN ('lost','failed','shutdown')", (team["lead_agent_id"],)).fetchone()
        if session:
            self._insert_team_message(connection, team=team, sender_type="runtime", recipient_type="lead",
                recipient_agent_id=team["lead_agent_id"], recipient_generation=session["generation"],
                message_type="VALIDATION_RESULT", payload={"candidate_id": "", "validation_run_id": identifier,
                    "integration_id": identifier, "status": status, "summary": summary,
                    "next_action": "Inspect validation logs with team_resolve_integration(action=inspect). Diagnose environment versus code failure before retry or repair."},
                dedupe_key=dedupe_key, created_at=utc_now_iso(), priority="control")
