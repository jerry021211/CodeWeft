from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from codeagent.multiagent.coordinator import Coordinator
from codeagent.multiagent.executions import ExecutionRepository
from codeagent.multiagent.maintenance import ExecutionResources
from codeagent.multiagent.leases import project_access
from codeagent.worktrees.snapshots import LocalGit
from codeagent.multiagent.verification import run_checks
from codeagent.runtime import CancellationToken
from codeagent.web.storage import SQLiteRepository


class NativeResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'project'
        self.source.mkdir()
        self.db = SQLiteRepository(self.root / 'state.db', recover_incomplete=False)
        self.addCleanup(self.db.close)
        self.conv = self.db.create_conversation(workspace=self.source)
        self.repo = ExecutionRepository(self.db)
        tl = self.db.ensure_conversation_task_list(self.conv.id)
        task = self.db.create_task(tl.id, subject='write', description='write',
            metadata={'execution': {'files': ['answer.txt'], 'isolation': 'isolated', 'checks': [
                {'argv': [sys.executable, '-c', "from pathlib import Path; assert Path('answer.txt').read_text() == '42'"]}]}})
        def worker(task, path, *args):
            (path / 'answer.txt').write_text('42')
            return {'ok': True, 'summary': 'done'}
        token = CancellationToken()
        c = Coordinator(self.repo, self.root / 'collaborations', self.conv.id, self.source,
            token, lambda *a: None, worker,
            lambda path, checks, *_: run_checks(path, checks, token, None, lambda _: None))
        self.value = c.start('goal', [task.task.id])
        self.assertEqual(self.value['status'], 'completed', self.value)
        self.resources = ExecutionResources(self.repo, self.root / 'collaborations', self.value['id'])

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.source), *args], stderr=subprocess.STDOUT)

    def test_archive_preserves_project_history_and_bundle_and_is_idempotent(self):
        before = (self.git('rev-parse', 'HEAD'), self.git('diff', '--cached'), self.git('status', '--porcelain'))
        self.git('update-ref', 'refs/codeagent/native/legacy-unowned', 'HEAD')
        self.assertGreater(self.resources.inspect()['bytes'], 0)
        result = self.resources.archive()
        self.assertEqual(result['files'], 0)
        self.assertEqual(self.resources.archive(), result)
        self.assertEqual((self.source / 'answer.txt').read_text(), '42')
        self.assertEqual(before, (self.git('rev-parse', 'HEAD'), self.git('diff', '--cached'), self.git('status', '--porcelain')))
        with zipfile.ZipFile(result['archive']['path']) as archive:
            self.assertIn('snapshots.bundle', archive.namelist())
            self.assertTrue(any(n.endswith('/answer.txt') for n in archive.namelist()))
            archive.extract('snapshots.bundle', self.root / 'verify')
        self.git('bundle', 'verify', str(self.root / 'verify/snapshots.bundle'))
        self.assertNotIn(str(self.resources.root).replace('\\', '/').encode(), self.git('worktree', 'list', '--porcelain'))
        refs = self.git('for-each-ref', '--format=%(refname)')
        self.assertIn(b'legacy-unowned', refs)
        for ref in self.repo.get(self.value['id']).get('owned_refs', {}):
            self.assertNotIn(ref.encode(), refs)

    def test_retention_pause_and_busy_project_preserve_resources(self):
        before = self.resources.inspect()['bytes']
        with self.assertRaisesRegex(ValueError, 'retention'):
            self.resources.archive(retention_days=30)
        with project_access(self.source, lambda: None):
            with self.assertRaisesRegex(ValueError, '稍后'):
                self.resources.archive()
        self.repo.change(self.value['id'], lambda r: r.update(status='paused'))
        with self.assertRaisesRegex(ValueError, '保留现场'):
            self.resources.archive()
        self.assertEqual(self.resources.inspect()['bytes'], before)

    def test_archive_failure_retains_files(self):
        before = self.resources.inspect()['bytes']
        with patch.object(zipfile.ZipFile, 'testzip', return_value='corrupt'):
            with self.assertRaisesRegex(ValueError, 'verification failed'):
                self.resources.archive()
        self.assertEqual(self.resources.inspect()['bytes'], before)
        self.assertIsNone(self.resources.inspect()['archive'])

    def test_links_outside_execution_are_rejected(self):
        link = self.resources.root / 'escape'
        try:
            link.symlink_to(self.source, target_is_directory=True)
        except OSError:
            # Windows unprivileged junction creation is available without developer mode.
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(self.source)], check=True, capture_output=True)
        self.addCleanup(lambda: link.unlink() if link.is_symlink() else link.rmdir())
        with self.assertRaises(ValueError):
            self.resources.archive()
        self.assertEqual((self.source / 'answer.txt').read_text(), '42')

    def test_changed_owned_ref_prevents_cleanup(self):
        ref = next(iter(self.repo.get(self.value['id'])['owned_refs']))
        self.repo.change(self.value['id'], lambda r: r['owned_refs'].update({ref: '0' * 40}))
        with self.assertRaisesRegex(ValueError, 'ref changed'):
            self.resources.archive()
        self.assertGreater(self.resources.inspect()['bytes'], 0)

    def test_changed_file_during_archive_preserves_workspace(self):
        path = next(self.resources.root.rglob('answer.txt'))
        def testzip(_):
            path.write_text('changed')
            return None
        with patch.object(zipfile.ZipFile, 'testzip', testzip):
            with self.assertRaisesRegex(ValueError, 'Files changed'):
                self.resources.archive()
        self.assertEqual(path.read_text(), 'changed')
        self.assertIsNone(self.resources.inspect()['archive'])

    def test_retry_after_partial_cleanup_retains_original_archive(self):
        original = LocalGit.run
        removals = []
        def run(git, *args, **kwargs):
            if args[:2] == ('worktree', 'remove'):
                removals.append(args[-1])
                if len(removals) == 2:
                    raise RuntimeError('simulated cleanup interruption')
            return original(git, *args, **kwargs)
        with patch.object(LocalGit, 'run', run):
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                self.resources.archive()
        first = self.resources.parent / 'archives' / (self.value['id'] + '.zip')
        saved = first.read_bytes()
        result = self.resources.archive()
        self.assertNotEqual(result['archive']['path'], str(first))
        self.assertEqual(saved, first.read_bytes())
        self.assertEqual(result['files'], 0)
