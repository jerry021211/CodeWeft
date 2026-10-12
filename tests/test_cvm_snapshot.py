from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from codeagent.cvm.snapshot import capture, same_version


class CvmSnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'src').mkdir()
        (self.root / 'src' / 'a.py').write_text('one')

    def test_non_git_content_add_delete_and_external_changes(self):
        before = capture(self.root, ('src',))
        self.assertEqual(before['quality'], 'complete')
        self.assertEqual(before['scanned_bytes'], 3)
        self.assertGreaterEqual(before['duration_ms'], 0)
        self.assertTrue(same_version(before, capture(self.root, ('src',))))
        (self.root / 'src' / 'a.py').write_text('two')
        self.assertFalse(same_version(before, capture(self.root, ('src',))))
        before = capture(self.root, ('src',))
        (self.root / 'src' / 'new.py').write_text('new')
        self.assertFalse(same_version(before, capture(self.root, ('src',))))
        before = capture(self.root, ('src',))
        (self.root / 'src' / 'new.py').unlink()
        self.assertFalse(same_version(before, capture(self.root, ('src',))))

    def test_cwd_and_scope_are_part_of_identity(self):
        root = capture(self.root, ('src',))
        nested = capture(self.root, ('.',), cwd=self.root / 'src')
        self.assertFalse(same_version(root, nested))

    def test_limits_and_unreadable_inputs_degrade(self):
        for kwargs in ({'max_bytes': 1}, {'max_files': 1}):
            self.assertEqual(capture(self.root, ('src',), **kwargs)['quality'], 'partial')
        with patch.object(Path, 'open', side_effect=PermissionError):
            self.assertEqual(capture(self.root, ('src',))['quality'], 'partial')
        self.assertEqual(capture(self.root, ())['quality'], 'partial')
        self.assertEqual(capture(self.root, ('../outside',))['quality'], 'partial')

    def test_secrets_are_not_read_and_generated_cache_is_excluded(self):
        cache = self.root / 'src' / '__pycache__'
        cache.mkdir()
        (cache / 'generated.pyc').write_text('generated')
        before = capture(self.root, ('src',))
        (cache / 'other.pyc').write_text('other')
        self.assertTrue(same_version(before, capture(self.root, ('src',))))
        (self.root / 'src' / '.env').write_text('DO_NOT_READ=secret')
        snapshot = capture(self.root, ('src',))
        self.assertEqual(snapshot['quality'], 'partial')
        self.assertNotIn('secret', str(snapshot))
        self.assertNotIn('src/.env', snapshot['files'])
