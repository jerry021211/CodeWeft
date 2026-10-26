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

    def test_worker_bytecode_is_not_delivered_and_check_import_does_not_block(self):
        def worker(t, path, *_):
            (path / 'module.py').write_text('VALUE = 42\n')
            subprocess.run([sys.executable, '-c', 'import module'], cwd=path, check=True)
            self.assertTrue(list(path.glob('__pycache__/*.pyc')))
            return {'ok': True, 'summary': 'done'}
        key = self.task('a', checks=[{'argv': [sys.executable, '-c', 'import module; assert module.VALUE == 42']}])
        result = self.coordinator(worker).start('goal', [key])
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue((self.source / 'module.py').exists())
        self.assertFalse((self.source / '__pycache__').exists())
        self.assertTrue(self.repo.get(result['id'])['artifacts'])

    def test_checks_still_detect_source_edits_inside_cache_named_directory(self):
        def worker(t, path, *_):
            (path / 'a.txt').write_text('done')
            return {'ok': True, 'summary': 'done'}
        check = "from pathlib import Path; p=Path('__pycache__'); p.mkdir(exist_ok=True); (p/'source.py').write_text('changed')"
        result = self.coordinator(worker).start('goal', [self.task('a', checks=[{'argv': [sys.executable, '-c', check]}])])
        self.assertEqual(result['status'], 'blocked', result)
        self.assertIn('changed source', result['issue']['reason'])
        self.assertFalse((self.source / 'a.txt').exists())

    def test_baseline_bytecode_is_protected_from_artifact_filter(self):
        self.baseline()
        cache = self.source / '__pycache__'
        cache.mkdir()
        (cache / 'user.pyc').write_bytes(b'user baseline')
        def worker(t, path, *_):
            (path / 'a.txt').write_text('done')
            return {'ok': True, 'summary': 'done'}
        check = "from pathlib import Path; Path('__pycache__/user.pyc').write_bytes(b'changed')"
        result = self.coordinator(worker).start('goal', [self.task('a', checks=[{'argv': [sys.executable, '-c', check]}])])
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual((cache / 'user.pyc').read_bytes(), b'user baseline')

    def test_failed_batch_member_does_not_replay_successful_sibling(self):
        calls = []
        def worker(t, path, *_):
            calls.append(t['subject'])
            if t['subject'] == 'b' and calls.count('b') == 1:
                return {'ok': False, 'summary': 'temporary failure'}
            (path / (t['subject'] + '.txt')).write_text('done')
            return {'ok': True, 'summary': 'done'}
        c = self.coordinator(worker)
        first = c.start('goal', [self.task('a'), self.task('b')])
        self.assertEqual(first['status'], 'blocked')
        final = c.run(first['id'])
        self.assertEqual(final['status'], 'completed', final)
        self.assertEqual(calls.count('a'), 1)
        self.assertEqual(calls.count('b'), 2)

    def test_failed_check_retries_only_failed_check_without_replaying_workers(self):
        calls, checks = [], []
        def worker(t, path, *_):
            calls.append(t['subject'])
            (path / (t['subject'] + '.txt')).write_text('done')
            return {'ok': True, 'summary': 'done'}
        def verify(path, commands, identifier, key):
            checks.append(key)
            if key == self.ids['b'] and checks.count(key) == 1:
                raise RuntimeError('test infrastructure temporarily unavailable')
        c = self.coordinator(worker, verify=verify)
        first = c.start('goal', [self.task('a'), self.task('b')])
        self.assertEqual(first['status'], 'blocked')
        final = c.run(first['id'])
        self.assertEqual(final['status'], 'completed', final)
        self.assertCountEqual(calls, ['a', 'b'])
        self.assertEqual(checks.count(self.ids['a']), 1)
        self.assertEqual(checks.count(self.ids['b']), 2)
        self.assertEqual(checks[-1], 'delivery')

    def test_repeated_retry_cannot_accept_source_mutated_by_failed_check(self):
        def worker(t, path, *_):
            (path / 'a.txt').write_text('reviewed')
            return {'ok': True, 'summary': 'done'}
        def verify(path, commands, identifier, key):
            (path / 'a.txt').write_text('not reviewed')
            raise RuntimeError('check failed after source mutation')
        c = self.coordinator(worker, verify=verify)
        first = c.start('goal', [self.task('a')])
        second = c.run(first['id'])
        self.assertEqual(second['status'], 'blocked')
        self.assertIn('retained verification input changed', second['issue']['reason'])
        self.assertEqual(second['records'][self.ids['a']]['attempts'], 1)
        self.assertFalse((self.source / 'a.txt').exists())

    def test_readonly_research_does_not_initialize_git(self):
        result = self.coordinator(lambda *a: {"ok": True, "summary": "research"}, read_only=True).start(
            "research", [self.task("a", profile="research", checks=[])])
        self.assertEqual(result["status"], "completed", result)
        self.assertFalse((self.source / ".git").exists())

    def test_service_restart_reconciles_native_tasks_without_replaying(self):
        first, second = self.task('done'), self.task('interrupted')
        record = self.repo.create(self.conv.id, self.source, 'goal', [first, second])
        def running(r):
            r['status'] = 'running'
            r['records'][first]['status'] = 'accepted'
            r['records'][second]['status'] = 'running'
            r['checkpoints'][second] = {'messages': ['retained']}
        self.repo.change(record['id'], running)
        self.service.close()
        with SQLiteRepository(self.root / 'state.db') as recovered:
            repo = ExecutionRepository(recovered)
            value = repo.get(record['id'])
            self.assertEqual(value['status'], 'paused')
            self.assertEqual(value['records'][first]['status'], 'accepted')
            self.assertEqual(value['records'][second]['status'], 'paused')
            self.assertEqual(value['checkpoints'][second], {'messages': ['retained']})
            task = recovered.get_task_resource(self.task_list.id, second).task
            self.assertEqual(task.status.value, 'pending')
            self.assertIsNone(task.owner)
            revision = value['revision']
            repo.recover_interrupted()
            self.assertEqual(repo.get(record['id'])['revision'], revision)

    def test_service_restart_preserves_unknown_writes_and_invalidates_epoch(self):
        key = self.task('a')
        value = self.repo.create(self.conv.id, self.source, 'goal', [key])
        def running(r):
            r['status'] = 'running'
            r['records'][key].update(status='running', inflight={'effect': 'write', 'tool': 'bash'})
        self.repo.change(value['id'], running)
        self.repo.recover_interrupted()
        recovered = self.repo.get(value['id'])
        self.assertEqual(recovered['status'], 'blocked')
        self.assertEqual(recovered['records'][key]['inflight']['tool'], 'bash')
        with self.assertRaisesRegex(ValueError, 'Stale execution'):
            self.repo.change(value['id'], lambda r: None, epoch=value['epoch'])
        with self.assertRaisesRegex(ValueError, 'unknown results'):
            self.coordinator(lambda *a: self.fail('must not replay')).run(value['id'])

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
        self.assertEqual(calls, [task])  # A checks-only correction retains the member's result.
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

    def interrupted_delivery(self):
        self.baseline()
        source = self.source
        class StopAfterFirstFile(CancellationToken):
            def raise_if_cancelled(self):
                if (source / 'a.txt').exists() and not (source / 'b.txt').exists():
                    self.cancel('interrupt publication')
                super().raise_if_cancelled()
        self.token = StopAfterFirstFile()
        def worker(t, path, *_):
            (path / 'a.txt').write_text('team a\n')
            (path / 'b.txt').write_text('team b\n')
            return {'ok': True, 'summary': 'done'}
        with self.assertRaises(CancelledError):
            self.coordinator(worker).start('goal', [self.task('a')])
        self.token = CancellationToken()
        return self.repo.list(self.conv.id, self.source)[0]

    def test_partial_delivery_revalidates_and_preserves_new_user_edits(self):
        record = self.interrupted_delivery()
        (self.source / 'user-note.txt').write_text('keep my note')
        (self.source / 'base.txt').write_text('user edited after pause')
        self.git('add', 'base.txt')
        head, index = self.git('rev-parse', 'HEAD'), self.git('diff', '--cached')
        verified = []
        def verify(path, checks, identifier, task):
            self.assertEqual(task, 'delivery')
            self.assertEqual((path / 'user-note.txt').read_text(), 'keep my note')
            self.assertEqual((path / 'base.txt').read_text(), 'user edited after pause')
            self.assertEqual((path / 'b.txt').read_text(), 'team b\n')
            verified.append(task)
        result = self.coordinator(lambda *a: self.fail('do not replay workers'), verify=verify).run(record['id'])
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(verified, ['delivery'])
        self.assertEqual((self.source / 'user-note.txt').read_text(), 'keep my note')
        self.assertEqual((self.source / 'b.txt').read_text(), 'team b\n')
        self.assertEqual((self.git('rev-parse', 'HEAD'), self.git('diff', '--cached')), (head, index))
        self.assertEqual(len(self.repo.get(record['id'])['delivery_history']), 1)

    def test_partial_delivery_conflict_can_be_abandoned_without_losing_files(self):
        record = self.interrupted_delivery()
        (self.source / 'a.txt').write_text('user replacement\n')
        c = self.coordinator(lambda *a: self.fail('do not replay workers'))
        result = c.run(record['id'])
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('conflict', result['issue']['reason'])
        self.assertEqual((self.source / 'a.txt').read_text(), 'user replacement\n')
        self.assertFalse((self.source / 'b.txt').exists())
        self.assertTrue(self.repo.get(record['id'])['delivery'])
        result = c.close(record['id'])
        self.assertEqual(result['status'], 'closed')
        self.assertIn('保留', result['closure_note'])
        archived = self.repo.get(record['id'])
        self.assertIsNone(archived['delivery'])
        self.assertEqual(archived['delivery_history'][-1]['disposition'], 'abandoned')
        self.assertEqual((self.source / 'a.txt').read_text(), 'user replacement\n')
        self.assertFalse((self.source / 'b.txt').exists())
        new = self.repo.create(self.conv.id, self.source, 'new', [self.task('new')])
        self.assertEqual(new['status'], 'pending')

    def test_failed_revalidation_keeps_prior_delivery_evidence(self):
        record = self.interrupted_delivery()
        (self.source / 'user-note.txt').write_text('keep')
        def fail(*args):
            raise RuntimeError('new validation failed')
        result = self.coordinator(lambda *a: self.fail('do not replay workers'), verify=fail).run(record['id'])
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.repo.get(record['id'])['delivery'], record['delivery'])
        self.assertEqual((self.source / 'user-note.txt').read_text(), 'keep')
        self.assertFalse((self.source / 'b.txt').exists())


if __name__ == "__main__":
    unittest.main()
