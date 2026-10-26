"""Persistent timed prompts and atomic, at-most-once dispatch claims."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4


SCHEDULE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, workspace TEXT NOT NULL,
    prompt TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('once', 'interval')),
    run_at TEXT NOT NULL, interval_seconds INTEGER,
    read_only INTEGER NOT NULL, enabled INTEGER NOT NULL,
    next_run_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS schedules_due ON schedules(enabled, next_run_at);
CREATE TABLE IF NOT EXISTS schedule_occurrences (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    scheduled_at TEXT NOT NULL, claimed_at TEXT NOT NULL,
    status TEXT NOT NULL, error TEXT,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    UNIQUE(schedule_id, scheduled_at)
);
CREATE INDEX IF NOT EXISTS schedule_occurrences_history
    ON schedule_occurrences(schedule_id, claimed_at);
"""


def timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("执行时间必须包含时区")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _schedule(row):
    result = dict(row)
    result["enabled"] = bool(result["enabled"])
    result["read_only"] = bool(result["read_only"])
    return result


class ScheduleStorage:
    def get_schedule(self, schedule_id):
        from codeagent.web.storage import RecordNotFoundError
        with self._lock:
            self._ensure_open()
            row = self._connection.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        if row is None:
            raise RecordNotFoundError("定时任务不存在")
        return _schedule(row)

    def list_schedules(self):
        with self._lock:
            self._ensure_open()
            return [_schedule(row) for row in self._connection.execute(
                "SELECT * FROM schedules ORDER BY created_at DESC, id DESC"
            ).fetchall()]

    def save_schedule(self, *, name, workspace, prompt, kind, run_at,
                      interval_seconds=None, read_only=True, enabled=True,
                      schedule_id=None, now=None):
        now = now or datetime.now(UTC)
        start = datetime.fromisoformat(run_at) if isinstance(run_at, str) else run_at
        start_text = timestamp(start)
        if not name.strip() or len(name) > 200 or not prompt.strip() or len(prompt) > 200_000:
            raise ValueError("任务名称或提示词为空或超过长度限制")
        if kind not in {"once", "interval"}:
            raise ValueError("不支持的定时任务类型")
        if kind == "interval":
            if type(interval_seconds) is not int or not 60 <= interval_seconds <= 31_536_000:
                raise ValueError("重复间隔必须在 60 秒到 365 天之间")
        elif interval_seconds is not None:
            raise ValueError("单次任务不能设置重复间隔")
        if enabled and start <= now:
            raise ValueError("首次执行时间必须晚于当前时间")
        identifier = schedule_id or f"schedule_{uuid4().hex}"
        current = timestamp(now)
        with self._transaction(immediate=True) as conn:
            if schedule_id:
                self.get_schedule(schedule_id)
                conn.execute(
                    "UPDATE schedules SET name=?, workspace=?, prompt=?, kind=?, run_at=?, "
                    "interval_seconds=?, read_only=?, enabled=?, next_run_at=?, updated_at=? WHERE id=?",
                    (name.strip(), workspace, prompt.strip(), kind, start_text, interval_seconds,
                     int(read_only), int(enabled), start_text if enabled else None, current, identifier),
                )
            else:
                conn.execute(
                    "INSERT INTO schedules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (identifier, name.strip(), workspace, prompt.strip(), kind, start_text,
                     interval_seconds, int(read_only), int(enabled), start_text if enabled else None,
                     current, current),
                )
        return self.get_schedule(identifier)

    def set_schedule_enabled(self, schedule_id, enabled, *, now=None):
        now = now or datetime.now(UTC)
        with self._transaction(immediate=True) as conn:
            item = self.get_schedule(schedule_id)
            next_run = item["next_run_at"]
            if enabled and not item["enabled"]:
                start = datetime.fromisoformat(item["run_at"])
                if start <= now:
                    if item["kind"] == "once":
                        raise ValueError("请先编辑单次任务，设置未来的执行时间")
                    step = timedelta(seconds=item["interval_seconds"])
                    start += ((now - start) // step + 1) * step
                next_run = timestamp(start)
            conn.execute("UPDATE schedules SET enabled=?, next_run_at=?, updated_at=? WHERE id=?",
                         (int(enabled), next_run, timestamp(now), schedule_id))
        return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id):
        with self._transaction(immediate=True) as conn:
            self.get_schedule(schedule_id)
            conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))

    def claim_due_schedule(self, *, now):
        """Advance before dispatch: crashes never replay an uncertain model/tool run."""
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=? "
                "ORDER BY next_run_at, id LIMIT 1", (timestamp(now),)
            ).fetchone()
            if row is None:
                return None
            item = _schedule(row)
            next_run = None
            if item["kind"] == "interval":
                start = datetime.fromisoformat(item["next_run_at"])
                step = timedelta(seconds=item["interval_seconds"])
                next_run = timestamp(start + ((now - start) // step + 1) * step)
            conn.execute("UPDATE schedules SET enabled=?, next_run_at=?, updated_at=? WHERE id=?",
                         (int(next_run is not None), next_run, timestamp(now), item["id"]))
            # Include dispatches still being admitted, queued runs and detached native work.
            busy = conn.execute(
                "SELECT 1 FROM schedule_occurrences o LEFT JOIN runs r ON r.conversation_id=o.conversation_id "
                "WHERE o.schedule_id=? AND (o.status='dispatching' OR r.status IN ('queued','running') "
                "OR EXISTS (SELECT 1 FROM agent_executions e WHERE e.conversation=o.conversation_id "
                "AND json_extract(e.data, '$.status') IN ('running','verifying','delivering'))) LIMIT 1",
                (item["id"],),
            ).fetchone()
            occurrence_id = f"occurrence_{uuid4().hex}"
            conn.execute(
                "INSERT INTO schedule_occurrences(id,schedule_id,scheduled_at,claimed_at,status,error) "
                "VALUES (?,?,?,?,?,?)",
                (occurrence_id, item["id"], item["next_run_at"], timestamp(now),
                 "skipped" if busy else "dispatching", "上一轮任务尚未结束" if busy else None),
            )
            return item, occurrence_id, bool(busy)

    def update_schedule_occurrence(self, occurrence_id, *, status, conversation_id=None, run_id=None, error=None):
        with self._transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE schedule_occurrences SET status=?, conversation_id=COALESCE(?,conversation_id), "
                "run_id=COALESCE(?,run_id), error=? WHERE id=?",
                (status, conversation_id, run_id, error, occurrence_id),
            )

    def recover_schedule_dispatches(self):
        with self._transaction(immediate=True) as conn:
            conn.execute("UPDATE schedule_occurrences SET status='interrupted', "
                         "error='服务在提交期间停止，结果可能不确定；请检查关联会话，不会自动重试' "
                         "WHERE status='dispatching'")

    def list_schedule_occurrences(self, schedule_id, *, limit=50):
        with self._lock:
            self.get_schedule(schedule_id)
            rows = self._connection.execute(
                "SELECT o.*, r.status AS run_status FROM schedule_occurrences o "
                "LEFT JOIN runs r ON r.id=o.run_id WHERE o.schedule_id=? "
                "ORDER BY o.claimed_at DESC, o.rowid DESC LIMIT ?",
                (schedule_id, min(200, max(1, limit))),
            ).fetchall()
            return [dict(row) for row in rows]
