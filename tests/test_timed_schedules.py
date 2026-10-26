from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

from codeagent.web.api import create_app
from codeagent.web.schedule_storage import timestamp
from codeagent.web.scheduler import RunScheduler
from codeagent.web.storage import RecordNotFoundError, SQLiteRepository
from codeagent.web.timed_scheduler import TimedScheduler
from codeagent.web.workspaces import WorkspaceCatalog
from tests.test_web_api import FakeScheduler
from tests.test_web_scheduler import _FakeFactory


class TimedSchedulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = SQLiteRepository(self.root / "state.db", recover_incomplete=False)
        self.scheduler = FakeScheduler(self.repo)
        self.timer = TimedScheduler(self.repo, self.scheduler, WorkspaceCatalog(self.root))
        self.now = datetime(2030, 1, 1, tzinfo=UTC)
        self.due = self.now + timedelta(minutes=5)

    def tearDown(self):
        self.timer.stop()
        self.repo.close()
        self.temp.cleanup()

    def create(self, **overrides):
        values = dict(name="检查项目", workspace=str(self.root), prompt="检查代码并报告",
                      kind="once", run_at=self.due, now=self.now)
        values.update(overrides)
        return self.repo.save_schedule(**values)

    def test_once_boundary_and_duplicate_ticks_create_exactly_one_run(self):
        task = self.create()
        self.timer.tick(self.due - timedelta(microseconds=1))
        self.assertEqual(self.scheduler.submissions, [])
        self.timer.tick(self.due)
        self.timer.tick(self.due + timedelta(days=1))
        history = self.repo.list_schedule_occurrences(task["id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["run_status"], "queued")
        self.assertEqual(self.scheduler.read_only_choices, [True])
        self.assertFalse(self.repo.get_schedule(task["id"])["enabled"])
        conversation = self.repo.get_conversation(history[0]["conversation_id"])
        self.assertEqual(conversation.workspace, str(self.root))
        self.assertEqual(self.scheduler.submissions[0][1], "检查代码并报告")

    def test_missed_intervals_coalesce_and_running_occurrence_is_skipped(self):
        task = self.create(kind="interval", interval_seconds=60, read_only=False)
        late = self.due + timedelta(hours=10, seconds=15)
        self.timer.tick(late)
        expected_next = self.due + timedelta(hours=10, minutes=1)
        self.assertEqual(self.repo.get_schedule(task["id"])["next_run_at"], timestamp(expected_next))
        self.assertEqual(len(self.scheduler.submissions), 1)
        self.timer.tick(expected_next)
        history = self.repo.list_schedule_occurrences(task["id"])
        self.assertEqual(history[0]["status"], "skipped")
        self.repo.start_run(history[1]["run_id"])
        self.repo.update_run_status(history[1]["run_id"], "completed")
        self.timer.tick(expected_next + timedelta(minutes=1))
        history = self.repo.list_schedule_occurrences(task["id"])
        self.assertEqual(len(self.scheduler.submissions), 2)
        self.assertNotEqual(history[0]["conversation_id"], history[2]["conversation_id"])
        self.assertEqual(self.scheduler.read_only_choices, [False, False])

    def test_atomic_claim_across_connections(self):
        self.create()
        other = SQLiteRepository(self.root / "state.db", recover_incomplete=False)
        barrier = threading.Barrier(2)
        def claim(repo):
            barrier.wait()
            return repo.claim_due_schedule(now=self.due)
        try:
            with ThreadPoolExecutor(2) as pool:
                claims = list(pool.map(claim, [self.repo, other]))
            self.assertEqual(sum(item is not None for item in claims), 1)
        finally:
            other.close()

    def test_pause_resume_and_edit_preserve_history(self):
        task = self.create(kind="interval", interval_seconds=60)
        self.repo.set_schedule_enabled(task["id"], False, now=self.now)
        self.timer.tick(self.due)
        self.assertEqual(self.scheduler.submissions, [])
        resumed = self.repo.set_schedule_enabled(task["id"], True, now=self.due)
        self.assertEqual(resumed["next_run_at"], timestamp(self.due + timedelta(minutes=1)))
        self.timer.tick(self.due + timedelta(minutes=1))
        self.create(schedule_id=task["id"], name="编辑后", run_at=self.due + timedelta(days=1))
        self.assertEqual(len(self.repo.list_schedule_occurrences(task["id"])), 1)
        self.assertEqual(self.repo.get_schedule(task["id"])["name"], "编辑后")

    def test_restart_preserves_tasks_and_does_not_replay_uncertain_dispatch(self):
        once = self.create()
        periodic = self.create(kind="interval", interval_seconds=60)
        self.repo.claim_due_schedule(now=self.due)
        self.repo.claim_due_schedule(now=self.due)
        self.repo.close()
        self.repo = SQLiteRepository(self.root / "state.db")
        self.scheduler = FakeScheduler(self.repo)
        self.timer = TimedScheduler(self.repo, self.scheduler, WorkspaceCatalog(self.root))
        self.repo.recover_schedule_dispatches()
        self.timer.tick(self.due)
        self.assertEqual(self.scheduler.submissions, [])
        self.assertEqual(self.repo.list_schedule_occurrences(once["id"])[0]["status"], "interrupted")
        self.timer.tick(self.due + timedelta(minutes=1))
        self.assertEqual(len(self.scheduler.submissions), 1)
        self.assertEqual(len(self.repo.list_schedule_occurrences(periodic["id"])), 2)

    def test_restart_dispatch_gap_still_detects_active_run_by_conversation(self):
        task = self.create(kind="interval", interval_seconds=60)
        _, occurrence, _ = self.repo.claim_due_schedule(now=self.due)
        conversation = self.repo.create_conversation(workspace=self.root)
        self.repo.update_schedule_occurrence(occurrence, status="dispatching", conversation_id=conversation.id)
        self.repo.create_run(conversation.id)
        self.repo.recover_schedule_dispatches()
        self.timer.tick(self.due + timedelta(minutes=1))
        self.assertEqual(self.repo.list_schedule_occurrences(task["id"])[0]["status"], "skipped")

    def test_failure_is_visible_and_other_tasks_continue(self):
        bad = self.create(workspace=str(self.root / "missing"))
        good = self.create()
        with self.assertLogs("codeagent.web.timed_scheduler", level="ERROR"):
            self.timer.tick(self.due)
        self.assertEqual(self.repo.list_schedule_occurrences(bad["id"])[0]["status"], "failed")
        self.assertEqual(self.repo.list_schedule_occurrences(good["id"])[0]["status"], "submitted")
        self.timer.tick(self.due)
        self.assertEqual(len(self.scheduler.submissions), 1)

    def test_detached_collaboration_blocks_overlap_after_root_run_finishes(self):
        task = self.create(kind="interval", interval_seconds=60)
        self.timer.tick(self.due)
        occurrence = self.repo.list_schedule_occurrences(task["id"])[0]
        self.repo.start_run(occurrence["run_id"])
        self.repo.update_run_status(occurrence["run_id"], "completed")
        conversation = self.repo.get_conversation(occurrence["conversation_id"])
        with self.repo._transaction(immediate=True) as conn:
            conn.execute("INSERT INTO agent_executions VALUES (?, ?, ?, ?, ?)",
                         ("detached", conversation.id, conversation.active_task_list_id,
                          str(self.root), '{"status":"running"}'))
        self.timer.tick(self.due + timedelta(minutes=1))
        self.assertEqual(self.repo.list_schedule_occurrences(task["id"])[0]["status"], "skipped")
        self.assertEqual(len(self.scheduler.submissions), 1)

    def test_delete_preserves_generated_conversation_and_history_allows_conversation_deletion(self):
        task = self.create()
        self.timer.tick(self.due)
        occurrence = self.repo.list_schedule_occurrences(task["id"])[0]
        self.repo.start_run(occurrence["run_id"])
        self.repo.update_run_status(occurrence["run_id"], "completed")
        self.repo.delete_conversation(occurrence["conversation_id"])
        self.assertIsNone(self.repo.list_schedule_occurrences(task["id"])[0]["conversation_id"])
        another = self.create()
        self.timer.tick(self.due)
        other_occurrence = self.repo.list_schedule_occurrences(another["id"])[0]
        self.repo.delete_schedule(another["id"])
        self.assertIsNotNone(self.repo.get_conversation(other_occurrence["conversation_id"]))
        with self.assertRaises(RecordNotFoundError):
            self.repo.get_schedule(another["id"])

    def test_timezone_normalized_and_invalid_schedules_rejected(self):
        item = self.create(run_at="2030-01-01T08:05:00+08:00")
        self.assertEqual(item["run_at"], timestamp(self.due))
        for values in [dict(run_at="2030-01-01T00:05:00"), dict(run_at=self.now),
                       dict(name=" "), dict(prompt=" "), dict(kind="cron"),
                       dict(interval_seconds=60), dict(kind="interval", interval_seconds=59),
                       dict(kind="interval", interval_seconds=True)]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.create(**values)
        self.timer.tick(self.due)
        with self.assertRaises(ValueError):
            self.repo.set_schedule_enabled(item["id"], True, now=self.due)

    @unittest.skipIf(TestClient is None, "FastAPI test dependencies are not installed")
    def test_real_run_queue_completes_and_shutdown_stops_timer_first(self):
        factory = _FakeFactory()
        scheduler = RunScheduler(self.repo, factory)
        app = create_app(repository=self.repo, scheduler=scheduler, workspace=self.root,
                         env=SimpleNamespace(data_dir=self.root / "runtime"), static_dir=self.root / "none")
        task = self.create(run_at=datetime.now(UTC) - timedelta(seconds=1),
                           now=datetime.now(UTC) - timedelta(seconds=2))
        with TestClient(app):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                history = self.repo.list_schedule_occurrences(task["id"])
                if history and history[0]["run_status"] == "completed":
                    break
                time.sleep(0.01)
            else:
                self.fail("Timed prompt did not complete through the run queue")
            self.assertEqual(factory.read_only_choices, [True])
            messages = self.repo.list_messages(history[0]["conversation_id"])
            self.assertTrue(any("答复:" in str(message.content) for message in messages))
        self.assertIsNone(app.state.timed_scheduler._thread)
        self.assertTrue(scheduler._closed)
        self.assertTrue(self.repo.health_check())


@unittest.skipIf(TestClient is None, "FastAPI test dependencies are not installed")
class ScheduleApiTests(unittest.TestCase):
    def test_crud_validation_and_session_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with SQLiteRepository(root / "state.db", recover_incomplete=False) as repo:
                scheduler = FakeScheduler(repo)
                app = create_app(repository=repo, scheduler=scheduler, workspace=root,
                                 env=SimpleNamespace(data_dir=root / "runtime"), static_dir=root / "none")
                body = dict(name="巡检", workspace=str(root), prompt="检查项目", kind="interval",
                            interval_seconds=3600, run_at=(datetime.now(UTC) + timedelta(days=1)).isoformat())
                with TestClient(app) as client:
                    self.assertEqual(client.post("/api/schedules", json=body).status_code, 403)
                    client.get("/api/health")
                    result = client.post("/api/schedules", json=body)
                    self.assertEqual(result.status_code, 201, result.text)
                    item = result.json()
                    path = f"/api/schedules/{item['id']}"
                    self.assertTrue(item["read_only"])
                    self.assertEqual(len(client.get("/api/schedules").json()), 1)
                    self.assertEqual(client.get(path + "/history").json(), [])
                    self.assertFalse(client.patch(path, json={"enabled": False}).json()["enabled"])
                    self.assertTrue(client.patch(path, json={"enabled": True}).json()["enabled"])
                    self.assertEqual(client.put(path, json={**body, "name": "新名称"}).json()["name"], "新名称")
                    for change in [{"interval_seconds": 0}, {"interval_seconds": True},
                                   {"run_at": "2030-01-01T09:00:00"}, {"prompt": " "},
                                   {"workspace": str(root / "missing")}, {"unknown": True}]:
                        with self.subTest(change=change):
                            self.assertEqual(client.post("/api/schedules", json={**body, **change}).status_code, 422)
                    self.assertEqual(client.post("/api/schedules", json=body, headers={"Origin": "https://evil.example"}).status_code, 403)
                    self.assertEqual(client.delete(path).status_code, 204)
                    self.assertEqual(client.get(path + "/history").status_code, 404)
                    self.assertEqual(client.patch(path, json={"enabled": True}).status_code, 404)
                    self.assertEqual(client.get("/api/schedules").json(), [])


if __name__ == "__main__":
    unittest.main()
