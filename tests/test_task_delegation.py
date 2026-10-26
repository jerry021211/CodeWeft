"""Task API, execution ownership, migration and the three delegation entry points."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from codeagent.multiagent.coordinator import Coordinator
from codeagent.multiagent.executions import ExecutionRepository
from codeagent.multiagent.migration import import_legacy
from codeagent.multiagent.tools import EXECUTION_TOOL_NAMES, register_execution_tools
from codeagent.runtime import CancellationToken
from codeagent.events import RunEvent
from codeagent.tools.registry import ToolRegistry
from codeagent.tools.tasks import create_task_tools
from codeagent.web.storage import SQLiteRepository


class TaskDelegationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = SQLiteRepository(self.root / 'web.db', recover_incomplete=False)
        self.addCleanup(self.db.close)
        self.conv = self.db.create_conversation(workspace=self.root)
        self.tl = self.db.ensure_conversation_task_list(self.conv.id)
        self.repo = ExecutionRepository(self.db)
        self.events = []

    def task(self, title, *, deps=(), **config):
        config.setdefault('profile', 'research')
        return self.db.create_task(self.tl.id, subject=title, description='Requirements for ' + title,
                    blocked_by=deps, metadata={'execution': config}).task.id

    def runtime(self, worker, verify=lambda *a: None):
        return Coordinator(self.repo, self.root / 'runtime', self.conv.id, self.root, CancellationToken(),
            lambda *a: self.events.append(a), worker, verify)

    def tools(self, worker):
        registry = ToolRegistry()
        for tool in create_task_tools(self.db, self.tl.id, conversation_id=self.conv.id):
            registry.register(tool)
        c = self.runtime(worker)
        register_execution_tools(registry, c, c.publish)
        return registry

    def test_tools_use_one_task_list_and_do_not_accept_duplicate_definitions(self):
        tools = self.tools(lambda *a: {'ok': True, 'summary': 'inspected'})
        created = json.loads(tools.execute('TaskCreate', dict(subject='Read config', description='Inspect config',
                        metadata={'execution': {'profile': 'research'}})))
        key = created['id']
        result = json.loads(tools.execute('delegate_task', {'taskId': key}))
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['task_list_id'], self.tl.id)
        listed = json.loads(tools.execute('TaskList', {}))
        self.assertEqual(len(listed), 1)
        self.assertEqual(self.db.get_task_resource(self.tl.id, key).task.status.value, 'completed')
        self.assertNotIn('collaborate', tools)
        self.assertTrue(EXECUTION_TOOL_NAMES <= {s['name'] for s in tools.schemas()})
        stored = json.loads(self.db._connection.execute('SELECT data FROM agent_executions').fetchone()[0])
        self.assertNotIn('tasks', stored)
        self.assertEqual(stored['task_ids'], [key])

    def test_batch_has_real_parallel_workers_and_dependency_order(self):
        a, b = self.task('A'), self.task('B')
        d = self.task('D', deps=[a, b])
        barrier = threading.Barrier(2)
        finished = set()
        def worker(t, path, *_):
            self.assertEqual(path, self.root)
            current = self.db.get_task_resource(self.tl.id, t['id']).task
            self.assertEqual(current.status.value, 'in_progress')
            self.assertIsNotNone(current.owner)
            if t['id'] in {a, b}:
                barrier.wait(timeout=3)
                finished.add(t['id'])
            else:
                self.assertEqual(finished, {a, b})
                self.assertEqual(self.db.get_task_resource(self.tl.id, a).task.status.value, 'completed')
            return {'ok': True, 'summary': t['subject']}
        result = json.loads(self.tools(worker).execute('delegate_batch', {'taskIds': [a, b, d]}))
        self.assertEqual(result['status'], 'completed', result)
        self.assertFalse((self.root / '.git').exists())
        self.assertEqual(len(self.db.list_task_resources(self.tl.id)), 3)

    def test_single_write_uses_current_workspace_without_git_delivery(self):
        key = self.task('Write', profile='implement', files=['a.txt'])
        def worker(t, path, *_):
            (path / 'a.txt').write_text('direct')
            return {'ok': True, 'summary': 'Written; no tests declared'}
        result = json.loads(self.tools(worker).execute('delegate_task', {'taskId': key}))
        self.assertEqual(result['mode'], 'single')
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual((self.root / 'a.txt').read_text(), 'direct')
        self.assertFalse((self.root / '.git').exists())

    def test_two_conversations_serialize_the_entire_direct_write(self):
        other = self.db.create_conversation(workspace=self.root)
        other_list = self.db.ensure_conversation_task_list(other.id)
        second = self.db.create_task(other_list.id, subject='second', description='increment',
            metadata={'execution': {'profile': 'implement', 'files': ['counter.txt']}}).task.id
        first = self.task('first', profile='implement', files=['counter.txt'])
        counter = self.root / 'counter.txt'
        counter.write_text('0')
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        def worker(t, path, *_):
            value = int(counter.read_text())
            if t['subject'] == 'first':
                entered.set()
                self.assertTrue(release.wait(5))
            counter.write_text(str(value + 1))
            return {'ok': True, 'summary': 'incremented'}
        def emit(kind, value):
            if (value.get('issue') or {}).get('owner') == 'runtime':
                waiting.set()
        c1 = self.runtime(worker)
        c2 = Coordinator(self.repo, self.root / 'another-runtime', other.id, self.root,
            CancellationToken(), emit, worker, lambda *a: None)
        with ThreadPoolExecutor(2) as pool:
            one = pool.submit(c1.start, 'g', [first], mode='single')
            try:
                self.assertTrue(entered.wait(5))
                two = pool.submit(c2.start, 'g', [second], mode='single')
                self.assertTrue(waiting.wait(5))
                self.assertEqual(counter.read_text(), '0')
            finally:
                release.set()
            self.assertEqual(one.result(5)['status'], 'completed')
            self.assertEqual(two.result(5)['status'], 'completed')
        self.assertEqual(counter.read_text(), '2')

    def test_project_lease_wait_can_be_cancelled(self):
        from codeagent.multiagent.leases import project_access
        from codeagent.runtime.cancellation import CancelledError
        token = CancellationToken()
        with project_access(self.root, lambda: None):
            with self.assertRaises(CancelledError):
                with project_access(self.root, token.raise_if_cancelled, lambda: token.cancel('stop waiting')):
                    self.fail('must not enter another live project lease')
        with project_access(self.root, lambda: None):
            pass

    def test_project_lease_is_shared_across_processes_and_released_on_exit(self):
        from codeagent.multiagent.leases import project_access
        from codeagent.runtime.cancellation import CancelledError
        script = '''
import sys
from codeagent.multiagent.leases import project_access
with project_access(sys.argv[1], lambda: None):
    print('owned', flush=True)
    sys.stdin.read(1)
'''
        process = subprocess.Popen([sys.executable, '-c', script, str(self.root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'owned')
            token = CancellationToken()
            with self.assertRaises(CancelledError):
                with project_access(self.root, token.raise_if_cancelled, lambda: token.cancel('stop')):
                    self.fail('another process still owns this project')
            # Simulate service termination, including exit without a normal lease release.
            process.terminate()
            process.communicate(timeout=5)
            with project_access(self.root, lambda: None):
                pass
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_append_rechecks_mode_and_isolation_without_partial_changes(self):
        first = self.task('first', profile='implement', files=['a.txt'])
        isolated = self.task('isolated', profile='implement', files=['b.txt'], isolation='isolated')
        value = self.repo.create(self.conv.id, self.root, 'goal', [first], mode='batch')
        with self.assertRaisesRegex(ValueError, 'execute_plan'):
            self.repo.append_tasks(value['id'], [isolated])
        self.assertEqual(self.repo.get(value['id'])['task_ids'], [first])
        extra = [self.task(str(i)) for i in range(5)]
        with self.assertRaisesRegex(ValueError, 'Task count'):
            self.repo.append_tasks(value['id'], extra)
        self.assertEqual(self.repo.get(value['id'])['task_ids'], [first])
        self.runtime(lambda *a: None).close(value['id'])
        single = self.repo.create(self.conv.id, self.root, 'goal', [extra[0]], mode='single')
        with self.assertRaisesRegex(ValueError, 'Task count'):
            self.repo.append_tasks(single['id'], [extra[1]])

    def test_task_edit_and_legacy_resume_cannot_drop_isolation(self):
        key = self.task('first', profile='implement', files=['a.txt'])
        value = self.repo.create(self.conv.id, self.root, 'goal', [key], mode='single')
        metadata = {'execution': {'profile': 'implement', 'files': ['a.txt'], 'isolation': 'isolated'}}
        with self.assertRaisesRegex(ValueError, 'execute_plan'):
            self.db.update_task(self.tl.id, key, changes={'metadata': metadata})
        # Simulate a record produced by an older runtime that lacked the edit gate.
        with self.db._transaction(immediate=True) as connection:
            connection.execute('UPDATE tasks SET metadata_json=? WHERE task_list_id=? AND id=?',
                               (json.dumps(metadata), self.tl.id, key))
        with self.assertRaisesRegex(ValueError, 'execute_plan'):
            self.runtime(lambda *a: self.fail('must not run in original directory')).run(value['id'])

    def test_execution_owned_task_cannot_be_completed_by_a_worker_before_validation(self):
        key = self.task('A')
        def worker(t, path, *_):
            with self.assertRaisesRegex(ValueError, 'executing'):
                self.db.update_task(self.tl.id, key, changes={'status': 'completed'}, human_override=True)
            return {'ok': True, 'summary': 'done'}
        self.assertEqual(self.runtime(worker).start('g', [key], mode='single')['status'], 'completed')

    def test_failed_check_keeps_dependency_locked_and_activity_records_reason(self):
        a = self.task('A', profile='verify', checks=[{'argv': ['python', '--version']}])
        b = self.task('B', deps=[a])
        calls = []
        def worker(t, path, *_):
            calls.append(t['id'])
            return {'ok': True, 'summary': 'done'}
        def fail(*a):
            raise ValueError('check did not pass')
        result = self.runtime(worker, fail).start('g', [a, b], mode='batch')
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(calls, [a])
        self.assertNotEqual(self.db.get_task_resource(self.tl.id, a).task.status.value, 'completed')
        self.assertIsNone(self.db.get_task_resource(self.tl.id, b).task.owner)
        self.assertGreater(len(self.db.list_task_activity(self.tl.id)), 2)

    def test_ordinary_tasks_are_not_subject_to_execution_ownership(self):
        key = self.task('ordinary')
        self.db.update_task(self.tl.id, key, changes={'status': 'in_progress'}, actor_owner='root')
        self.db.update_task(self.tl.id, key, changes={'status': 'completed'}, actor_owner='root')
        self.assertEqual(self.db.get_task_resource(self.tl.id, key).task.status.value, 'completed')

    def test_source_artifacts_are_rejected_and_trailing_directory_slashes_are_valid(self):
        with self.assertRaisesRegex(ValueError, 'artifacts'):
            self.task('bad', artifacts=['README.md'])
        key = self.task('ok', files=['tests/'])
        self.assertEqual(self.repo.inputs(self.tl.id, [key])[0]['files'], ['tests'])

    def test_completed_external_prerequisite_and_unfinished_external_blocker(self):
        a = self.task('A')
        b = self.task('B', deps=[a])
        c = self.runtime(lambda *a: {'ok': True, 'summary': 'done'})
        with self.assertRaisesRegex(ValueError, 'dependency'):
            c.start('g', [b])
        self.db.update_task(self.tl.id, a, changes={'status': 'completed'}, actor_owner='root')
        self.assertEqual(c.start('g', [b], mode='single')['status'], 'completed')

    def test_closed_failed_execution_cancels_tasks_without_removing_files(self):
        key = self.task('A')
        c = self.runtime(lambda *a: {'ok': False, 'reason': 'failed'})
        result = c.start('g', [key], mode='single')
        c.close(result['id'])
        self.assertEqual(self.db.get_task_resource(self.tl.id, key).task.status.value, 'cancelled')

    def test_runtime_and_task_changes_roll_back_together(self):
        key = self.task('A')
        run = self.repo.create(self.conv.id, self.root, 'g', [key])
        def bad_update(value):
            value['records'][key]['status'] = 'running'
            value['unserializable'] = object()
        with self.assertRaises(TypeError):
            self.repo.change(run['id'], bad_update)
        self.assertEqual(self.db.get_task_resource(self.tl.id, key).task.status.value, 'pending')
        self.assertEqual(self.repo.get(run['id'])['records'][key]['status'], 'pending')

    def test_activity_replay_preserves_execution_snapshots_members_and_checks(self):
        run = self.db.create_run(self.conv.id)
        kinds = ['collaboration.snapshot', 'collaboration.member', 'collaboration.check']
        for kind in kinds:
            self.db.append_event(RunEvent(type=kind, conversation_id=self.conv.id, run_id=run.id,
                                         payload={'task_id': '1'}))
        self.assertEqual([e.type for e in self.db.list_events(run.id, activity_only=True)], kinds)

    def test_explicit_isolation_is_not_silently_ignored_by_direct_delegation(self):
        key = self.task('isolated', isolation='isolated')
        with self.assertRaisesRegex(ValueError, 'execute_plan'):
            self.runtime(lambda *a: self.fail('must not start')).start('g', [key], mode='single')
        self.assertEqual(self.repo.list(self.conv.id, self.root), [])

    def test_legacy_import_is_idempotent_retains_checkpoints_and_removes_exclusion_semantics(self):
        path = self.root / 'old.sqlite'
        old = sqlite3.connect(path)
        old.executescript('CREATE TABLE collaborations(conversation TEXT, workspace TEXT, data TEXT); '
                          'CREATE TABLE root_recovery(run TEXT, conversation TEXT, data TEXT);')
        legacy = dict(id='collab_old', engine='native_v1', conversation=self.conv.id, workspace=str(self.root),
            objective='g', version=1, status='running', revision=2, epoch=1, read_only=False,
            tasks=[dict(id='cli', objective='implement', profile='implement', files=['cli.py'], artifacts=['cli.py'],
                        depends_on=[], checks=[])],
            records={'cli': dict(status='running', attempts=1, summary='', inflight={})},
            checkpoints={'cli': {'messages': ['preserved']}}, workspaces={'shared': str(self.root / 'shared')},
            delivery=None, issue=None)
        old.execute('INSERT INTO collaborations VALUES (?,?,?)', (self.conv.id, str(self.root), json.dumps(legacy)))
        old.commit()
        old.close()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        import_legacy(self.db, path, self.conv.id, self.root)
        import_legacy(self.db, path, self.conv.id, self.root)
        tasks = self.db.list_task_resources(self.tl.id)
        self.assertEqual(len(tasks), 1)
        key = tasks[0].task.id
        result = self.repo.get('collab_old')
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['checkpoints'][key], {'messages': ['preserved']})
        self.assertEqual(result['records'][key]['member_id'], 'collab_old_cli')
        self.assertNotIn('artifacts', tasks[0].task.metadata['execution'])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)


if __name__ == '__main__':
    unittest.main()
