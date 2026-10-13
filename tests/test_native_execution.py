"""Real local Git/process integration, with deterministic workers instead of an API."""
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codeagent.multiagent.coordinator import Coordinator
from codeagent.multiagent.executions import ExecutionRepository
from codeagent.web.storage import SQLiteRepository
from codeagent.multiagent.verification import run_checks
from codeagent.runtime import CancellationToken
from codeagent.runtime.cancellation import CancelledError


class NativeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.service = SQLiteRepository(self.root / 'state.db', recover_incomplete=False)
        self.addCleanup(self.service.close)
        self.conv = self.service.create_conversation(workspace=self.source)
        self.task_list = self.service.ensure_conversation_task_list(self.conv.id)
        self.repo = ExecutionRepository(self.service)
        self.ids = {}
        self.token = CancellationToken()
        self.events = []

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.source), *args], stderr=subprocess.STDOUT).decode().strip()

    def baseline(self):
        self.git("init")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.source / "base.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")

    def coordinator(self, worker, *, read_only=False, verify=None):
        return Coordinator(self.repo, self.root / "workspaces", self.conv.id, self.source, self.token,
                           lambda *e: self.events.append(e), worker,
                           verify or (lambda path, checks, *_: run_checks(path, checks, self.token, None, lambda _: None)),
                           read_only=read_only)

    def task(self, key, **kwargs):
        deps = [self.ids[d] for d in kwargs.pop('depends_on', [])]
        config = dict(files=[key + '.txt'], checks=[{'argv': [sys.executable, '-c', "print('verified')"]}])
        config.update(kwargs)
        task = self.service.create_task(self.task_list.id, subject=key, description=key,
                    blocked_by=deps, metadata={'execution': config})
        self.ids[key] = task.task.id
        return task.task.id

    def test_shared_dependencies_dirty_baseline_and_local_delivery(self):
        self.baseline()
        (self.source / "base.txt").write_text("user staged\n")
        self.git("add", "base.txt")
        (self.source / "base.txt").write_text("user unstaged\n")
        (self.source / "mine.txt").write_text("untracked\n")
        before_head, before_index = self.git("rev-parse", "HEAD"), self.git("diff", "--cached")
        locations = []
        def worker(t, path, *_):
            locations.append(path)
            self.assertEqual((path / "base.txt").read_text(), "user unstaged\n")
            if t["subject"] == "b":
                self.assertEqual((path / "a.txt").read_text(), "a\n")
            (path / (t["subject"] + ".txt")).write_text(t["subject"] + "\n")
            return {"ok": True, "summary": "done"}
        result = self.coordinator(worker).start("goal", [self.task("a"), self.task("b", depends_on=["a"])])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(locations[0], locations[1])
        self.assertEqual((self.source / "b.txt").read_text(), "b\n")
        self.assertEqual((self.source / "mine.txt").read_text(), "untracked\n")
        self.assertEqual(before_head, self.git("rev-parse", "HEAD"))
        self.assertEqual(before_index, self.git("diff", "--cached"))

    def test_isolated_result_is_merged_then_delivered(self):
        self.baseline()
        def worker(t, path, *_):
            (path / "a.txt").write_text("isolated\n")
            return {"ok": True, "summary": "done"}
        result = self.coordinator(worker).start("goal", [self.task("a", isolation="isolated")])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual((self.source / "a.txt").read_text(), "isolated\n")

    def test_readonly_research_does_not_initialize_git(self):
        result = self.coordinator(lambda *a: {"ok": True, "summary": "research"}, read_only=True).start(
            "research", [self.task("a", profile="research", checks=[])])
        self.assertEqual(result["status"], "completed", result)
        self.assertFalse((self.source / ".git").exists())

    def test_failed_check_does_not_unlock_dependent_or_deliver(self):
        calls = []
        def worker(t, path, *_):
            calls.append(t["subject"])
            (path / "a.txt").write_text("draft\n")
            return {"ok": True, "summary": "done"}
        task = self.task("a")
        self.service.update_task(self.task_list.id, task, changes={"metadata": {"execution": {"files": ["a.txt"], "checks": [{"argv": [sys.executable, "-c", "raise SystemExit(1)"]}]}}})
        result = self.coordinator(worker).start("goal", [task, self.task("b", depends_on=["a"])])
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(calls, ["a"])
        self.assertFalse((self.source / "a.txt").exists())

    def test_cancel_then_explicit_resume_reuses_checkpoint_and_workspace(self):
        paths = []
        def worker(t, path, checkpoint, save, *_):
            paths.append(path)
            if not checkpoint:
                (path / "a.txt").write_text("retained\n")
                save({"completed_step": 1})
                self.token.cancel("test pause")
                self.token.raise_if_cancelled()
            self.assertEqual(checkpoint, {"completed_step": 1})
            self.assertEqual((path / "a.txt").read_text(), "retained\n")
            return {"ok": True, "summary": "continued"}
        c = self.coordinator(worker)
        with self.assertRaises(CancelledError):
            c.start("goal", [self.task("a")])
        record = self.repo.list(self.conv.id, self.source)[0]
        self.assertEqual(record["status"], "paused")
        self.token = CancellationToken()
        result = self.coordinator(worker).run(record["id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(paths[0], paths[1])

    def test_user_edit_during_delivery_validation_is_preserved(self):
        self.baseline()
        def worker(t, path, *_):
            (path / "a.txt").write_text("team\n")
            return {"ok": True, "summary": "done"}
        def verify(path, checks, identifier, task):
            if task == "delivery":
                (self.source / "base.txt").write_text("new user edit\n")
        result = self.coordinator(worker, verify=verify).start("goal", [self.task("a")])
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual((self.source / "base.txt").read_text(), "new user edit\n")
        self.assertFalse((self.source / "a.txt").exists())

    def test_task_update_revises_failed_execution_without_a_second_plan(self):
        calls = []
        def worker(t, path, *_):
            calls.append(t['id'])
            (path / 'a.txt').write_text('done')
            return {'ok': True, 'summary': 'done'}
        def verify(path, checks, identifier, task):
            if checks[0] == 'old command':
                raise RuntimeError('environment command changed')
        task = self.task('a', checks=['old command'])
        c = self.coordinator(worker, verify=verify)
        result = c.start('goal', [task])
        self.assertEqual(result['status'], 'blocked')
        self.service.update_task(self.task_list.id, task, changes={'metadata': {
            'execution': {'files': ['a.txt'], 'checks': ['new command']}}})
        result = c.run(result['id'])
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(calls, [task, task])
        self.assertEqual(len(self.service.list_task_resources(self.task_list.id)), 1)

    def test_unknown_worker_write_cannot_be_blindly_replayed(self):
        def worker(t, path, checkpoint, save, operation, *_):
            operation({"effect": "write", "tool": "bash"})
            raise RuntimeError("interrupted")
        c = self.coordinator(worker)
        result = c.start("goal", [self.task("a")])
        with self.assertRaisesRegex(ValueError, "unknown results"):
            c.run(result["id"])

    def test_accepted_task_can_update_checks_without_rerunning_worker(self):
        calls = []
        def worker(t, path, *_):
            calls.append(t['id'])
            (path / 'a.txt').write_text('done')
            return {'ok': True, 'summary': 'done'}
        key = self.task('a', checks=[])
        c = self.coordinator(worker)
        result = c.start('goal', [key])
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['records'][key]['status'], 'accepted')
        self.service.update_task(self.task_list.id, key, changes={'metadata': {
            'execution': {'files': ['a.txt'], 'checks': [{'argv': [sys.executable, '-c', "print('ok')"]}]}}})
        result = c.run(result['id'])
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(calls, [key])
        self.assertEqual((self.source / 'a.txt').read_text(), 'done')

    def test_follow_up_task_repairs_accepted_code_in_same_plan(self):
        calls = []
        def worker(t, path, *_):
            calls.append(t['id'])
            (path / 'a.txt').write_text(t['subject'])
            return {'ok': True, 'summary': 'done'}
        first = self.task('a', checks=[])
        c = self.coordinator(worker)
        result = c.start('goal', [first])
        self.assertEqual(result['status'], 'blocked')
        fix = self.task('fix', depends_on=['a'], files=['a.txt'])
        self.repo.append_tasks(result['id'], [fix])
        result = c.run(result['id'])
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(calls, [first, fix])
        self.assertEqual((self.source / 'a.txt').read_text(), 'fix')
        self.assertEqual(len(self.service.list_task_resources(self.task_list.id)), 2)

    def test_process_validation_cancellation_exits_promptly(self):
        timer = threading.Timer(.3, lambda: self.token.cancel("stop process"))
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(CancelledError):
                run_checks(self.source, [{"argv": [sys.executable, "-c", "import time; time.sleep(20)"]}],
                           self.token, None, lambda _: None)
        finally:
            timer.join()
        self.assertLess(time.monotonic() - started, 6)

    def test_validation_permission_denial_never_executes_command(self):
        from codeagent.multiagent.verification import CheckFailed
        class Deny:
            def request(self, *args, **kwargs):
                return False
        target = self.source / "keep.txt"
        target.write_text("keep")
        with self.assertRaises(CheckFailed) as failure:
            run_checks(self.source, ["Remove-Item keep.txt"], self.token, Deny(), lambda _: None)
        self.assertEqual(failure.exception.evidence["category"], "authorization")
        self.assertTrue(target.exists())

    def test_interrupted_local_delivery_recovers_without_replaying_workers(self):
        calls = []
        source = self.source
        class StopAfterFirstFile(CancellationToken):
            def raise_if_cancelled(self):
                if (source / "a.txt").exists() and not (source / "b.txt").exists():
                    self.cancel("interrupt publication")
                super().raise_if_cancelled()
        self.token = StopAfterFirstFile()
        def worker(t, path, *_):
            calls.append(t["subject"])
            (path / "a.txt").write_text("a")
            (path / "b.txt").write_text("b")
            return {"ok": True, "summary": "done"}
        with self.assertRaises(CancelledError):
            self.coordinator(worker).start("goal", [self.task("a")])
        record = self.repo.list(self.conv.id, self.source)[0]
        self.assertTrue(record["delivery"])
        self.token = CancellationToken()
        result = self.coordinator(worker).run(record["id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(calls, ["a"])
        self.assertEqual((source / "b.txt").read_text(), "b")


if __name__ == "__main__":
    unittest.main()
