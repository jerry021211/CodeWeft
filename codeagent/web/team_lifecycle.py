"""Durable, all-or-nothing Team pause and recovery transitions."""
from __future__ import annotations

import json


class TeamLifecycleStorage:
    def request_team_pause(self, team_id, *, reason, command_id):
        from .storage import InvalidStateTransitionError, utc_now_iso
        now = utc_now_iso()
        with self._transaction(immediate=True) as db:
            team = self._team_row(db, team_id)
            if self._team_command_result(db, team_id, command_id, "pause_team") is not None:
                return
            if team["state"] in {"pausing", "paused"}:
                return
            if team["state"] != "running":
                raise InvalidStateTransitionError("只有运行中的团队可以暂停")
            metadata = json.loads(team["metadata_json"])
            snapshots = {}
            for attempt in db.execute("SELECT * FROM task_attempts WHERE team_run_id = ? "
                    "AND state NOT IN ('succeeded','failed','cancelled','orphaned')", (team_id,)):
                task = db.execute("SELECT revision FROM tasks WHERE task_list_id = ? AND id = ?",
                                  (attempt["task_list_id"], attempt["task_id"])).fetchone()
                session = db.execute("SELECT * FROM agent_sessions WHERE id = ?", (attempt["session_id"],)).fetchone()
                snapshots[attempt["id"]] = {"state": attempt["state"], "write_enabled": attempt["write_enabled"],
                    "task_revision": task["revision"], "session_state": session["state"],
                    "waiting_reason": session["waiting_reason"]}
            metadata["pause_attempts"] = snapshots
            db.execute("UPDATE team_runs SET state = 'pausing', metadata_json = ?, updated_at = ? WHERE id = ?",
                       (json.dumps(metadata), now, team_id))
            db.execute("UPDATE task_attempts SET write_enabled = 0 WHERE team_run_id = ?", (team_id,))
            db.execute("UPDATE worktree_bindings SET write_enabled = 0 WHERE team_run_id = ?", (team_id,))
            self._record_team_command(db, team_id, command_id, "pause_team", {"team_run_id": team_id}, now)
            self._append_team_event(db, team, "team.pausing", {"team_run_id": team_id, "reason": reason})
        self._notify_activity()

    def finish_team_pause(self, team_id):
        """Caller must first prove all in-process workers/Lead/validators exited."""
        from .storage import utc_now_iso
        with self._transaction(immediate=True) as db:
            team = self._team_row(db, team_id)
            if team["state"] != "pausing":
                return
            db.execute("UPDATE team_runs SET state = 'paused', updated_at = ? WHERE id = ?", (utc_now_iso(), team_id))
            self._append_team_event(db, team, "team.paused", {"team_run_id": team_id})
        self._notify_activity()

    def resume_team_run(self, team_id, *, fingerprints, reason, command_id):
        """Restore a stopped Team only after every retained Attempt passes checks.

        Filesystem checks are supplied by the supervisor; domain checks and all
        state changes share one transaction. A failed check cannot resume half a Team.
        """
        from .storage import StorageConflictError, utc_now_iso, _team_resources_overlap, _team_task_resource_keys
        now = utc_now_iso()
        with self._transaction(immediate=True) as db:
            team = self._team_row(db, team_id)
            if self._team_command_result(db, team_id, command_id, "resume_team") is not None:
                return
            if team["state"] == "running":
                return
            if team["state"] not in {"paused", "cancelled"}:
                raise StorageConflictError("团队尚未完全停止，请稍后再试")
            if db.execute("SELECT 1 FROM team_runs WHERE conversation_id = ? AND id != ? "
                          "AND state NOT IN ('completed','failed','cancelled','closed_with_unmerged_candidates')",
                          (team["conversation_id"], team_id)).fetchone():
                raise StorageConflictError("当前对话已有另一个活动团队，不能同时恢复旧团队")
            if team["active_plan_revision"] is None or self._team_plan_row(db, team_id, team["active_plan_revision"])["status"] != "approved":
                raise StorageConflictError("恢复需要仍然有效的已批准团队方案")
            metadata = json.loads(team["metadata_json"])
            saved = metadata.get("pause_attempts", {})
            legacy = team["state"] == "cancelled"
            attempts = db.execute("SELECT * FROM task_attempts WHERE team_run_id = ? ORDER BY created_at, id", (team_id,)).fetchall()
            restored = []
            for attempt in attempts:
                if attempt["state"] in {"succeeded", "failed"}:
                    continue
                snapshot = saved.get(attempt["id"])
                was_cancelled = legacy and attempt["state"] == "cancelled"
                if was_cancelled:
                    # The durable exit event is the authoritative pre-cancel state.
                    event = db.execute("SELECT payload_json FROM events WHERE run_id = ? AND type = 'team.attempt.cancelled' "
                        "AND json_extract(payload_json, '$.attempt_id') = ? ORDER BY seq DESC LIMIT 1",
                        (team["root_run_id"], attempt["id"])).fetchone()
                    assignment = db.execute("SELECT payload_json FROM team_messages WHERE attempt_id = ? "
                        "AND type = 'TASK_ASSIGNED' ORDER BY created_at LIMIT 1", (attempt["id"],)).fetchone()
                    if not event or not assignment or not attempt["worker_exited_at"]:
                        raise StorageConflictError(f"任务 {attempt['task_id']} 缺少完整停止记录，不能自动恢复")
                    original = snapshot["state"] if snapshot else json.loads(event["payload_json"])["previous_state"]
                    assigned_revision = metadata.get("resumed_task_revisions", {}).get(attempt["id"], json.loads(assignment["payload_json"])["task_revision"])
                    snapshot = {"state": original, "task_revision": assigned_revision + 1,
                                "session_state": "work" if original in {"running", "review_rejected", "plan_required"} else "waiting"}
                if snapshot is None:
                    if attempt["state"] == "orphaned":
                        raise StorageConflictError(f"任务 {attempt['task_id']} 的执行结果不确定，需要先核查工作现场")
                    if legacy and attempt["state"] not in {"cancelled", "orphaned"}:
                        raise StorageConflictError(f"任务 {attempt['task_id']} 尚未完成停止处理")
                    continue
                if attempt["result_unknown"] or attempt["state"] == "orphaned":
                    raise StorageConflictError(f"任务 {attempt['task_id']} 的执行结果不确定，需要先核查工作现场")
                if json.loads(attempt["error_json"] or "{}").get("type") == "team_plan_change_required":
                    raise StorageConflictError("任务需要重新批准方案，不能通过恢复扩大范围")
                task = db.execute("SELECT * FROM tasks WHERE task_list_id = ? AND id = ?",
                                  (attempt["task_list_id"], attempt["task_id"])).fetchone()
                if task["status"] not in ({"cancelled"} if was_cancelled else {"in_progress"}) or task["owner"] != attempt["agent_id"] or task["revision"] != snapshot["task_revision"]:
                    raise StorageConflictError(f"任务 {attempt['task_id']} 的归属或版本已变化")
                if attempt["team_plan_revision"] != team["active_plan_revision"] or not self._attempt_base_allowed(db, team, attempt):
                    raise StorageConflictError("任务方案或固定基线已失效")
                session = db.execute("SELECT * FROM agent_sessions WHERE id = ?", (attempt["session_id"],)).fetchone()
                if session["state"] not in {"waiting", "idle", "work"} or session["current_attempt_id"] not in {None, attempt["id"]}:
                    raise StorageConflictError("原成员会话已失效，请检查重启恢复记录")
                if db.execute("SELECT 1 FROM task_attempts WHERE team_run_id = ? AND id != ? "
                        "AND (task_id = ? OR agent_id = ?) AND state NOT IN ('succeeded','failed','cancelled','orphaned')",
                        (team_id, attempt["id"], attempt["task_id"], attempt["agent_id"])).fetchone():
                    raise StorageConflictError("任务或成员已有另一次执行")
                target = snapshot["state"]
                # A worker may have submitted its result while the pause request arrived.
                if not was_cancelled and attempt["state"] in {"plan_submitted", "candidate_submitted", "validating", "validation_failed", "committing"}:
                    target = attempt["state"]
                if target not in {"running", "review_rejected", "plan_required", "plan_submitted", "waiting", "candidate_submitted", "validating", "validation_failed"}:
                    raise StorageConflictError(f"任务停在 {target}，需要先核查提交或验证结果")
                task_meta = json.loads(task["metadata_json"])
                writing = task_meta.get("kind") == "code" and target in {"running", "review_rejected"}
                if writing:
                    self._ensure_attempt_plan_still_approved(db, attempt)
                if task_meta.get("kind") == "code":
                    binding = db.execute("SELECT * FROM worktree_bindings WHERE attempt_id = ?", (attempt["id"],)).fetchone()
                    if not binding or binding["state"] not in {"active", "frozen"} or fingerprints.get(attempt["id"]) != binding["fingerprint"] or binding["session_id"] != session["id"] or binding["generation"] != session["generation"]:
                        raise StorageConflictError("工作目录不存在、已清理或绑定发生变化")
                expected = _team_task_resource_keys(task_meta)
                held = db.execute("SELECT * FROM resource_leases WHERE team_run_id = ? AND attempt_id != ? AND state != 'released'", (team_id, attempt["id"])).fetchall()
                leases = db.execute("SELECT * FROM resource_leases WHERE attempt_id = ?", (attempt["id"],)).fetchall()
                if set(expected) != {(r["resource_kind"], r["resource_key"]) for r in leases}:
                    raise StorageConflictError("任务资源租约记录不完整")
                for kind, key in expected:
                    if any(_team_resources_overlap(kind, key, r["resource_kind"], r["resource_key"]) for r in held):
                        raise StorageConflictError(f"资源已被其他任务占用：{key}")
                working = target in {"running", "review_rejected", "plan_required"} and snapshot["session_state"] == "work"
                db.execute("UPDATE task_attempts SET state = ?, write_enabled = ?, cancel_requested_at = NULL, "
                    "worker_exited_at = NULL, finished_at = NULL, updated_at = ? WHERE id = ?",
                    (target, int(writing), now, attempt["id"]))
                db.execute("UPDATE worktree_bindings SET state = 'active', write_enabled = ?, frozen_reason = NULL WHERE attempt_id = ?", (int(writing), attempt["id"]))
                db.execute("UPDATE resource_leases SET state = 'active', released_at = NULL WHERE attempt_id = ?", (attempt["id"],))
                db.execute("UPDATE agent_sessions SET state = ?, current_attempt_id = ?, waiting_reason = ?, heartbeat_at = ?, updated_at = ? WHERE id = ?",
                    ("work" if working else "waiting", attempt["id"], None if working else snapshot.get("waiting_reason"), now, now, session["id"]))
                if was_cancelled:
                    db.execute("UPDATE tasks SET status = 'in_progress', updated_at = ? WHERE task_list_id = ? AND id = ?", (now, attempt["task_list_id"], attempt["task_id"]))
                    metadata.setdefault("resumed_task_revisions", {})[attempt["id"]] = task["revision"]
                db.execute("UPDATE team_messages SET acked_at = COALESCE(acked_at, ?) WHERE attempt_id = ? AND type = 'CANCEL'", (now, attempt["id"]))
                self._insert_team_message(db, team=team, sender_type="runtime", recipient_type="teammate",
                    recipient_agent_id=attempt["agent_id"], recipient_generation=session["generation"],
                    message_type="ATTEMPT_RESUMED", payload={"reason": reason, "do_not_replay": True,
                        "instruction": "继续现有工作；先检查文件和已执行工具结果，不要重复执行已完成操作。"},
                    dedupe_key=f"team-resume:{command_id}:{attempt['id']}", task_id=attempt["task_id"], attempt_id=attempt["id"], created_at=now, priority="control")
                restored.append(attempt["id"])
            metadata.pop("pause_attempts", None)
            db.execute("UPDATE team_runs SET state = 'running', metadata_json = ?, updated_at = ? WHERE id = ?", (json.dumps(metadata), now, team_id))
            self._advance_team_after_task_completion(db, self._team_row(db, team_id), now)
            # Only trials interrupted by this pause are automatically requeued.
            # Publishing/applying intents and real validation failures retain their recovery flow.
            db.execute("UPDATE team_integrations SET status = 'superseded' WHERE team_run_id = ? AND status = 'interrupted' AND error = 'team_paused'", (team_id,))
            self._recover_active_lead_sessions(db, now)
            self._record_team_command(db, team_id, command_id, "resume_team", {"attempt_ids": restored}, now)
            self._append_team_event(db, team, "team.resumed", {"team_run_id": team_id, "attempt_ids": restored, "reason": reason})
        self._notify_activity()
