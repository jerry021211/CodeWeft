import json
from pathlib import Path
import tempfile
from unittest import TestCase
from codeagent.web.storage import SQLiteRepository, StorageConflictError
from codeagent.multiagent.executions import ExecutionRepository


class RecoveryDataTests(TestCase):
    def test_delete_cascades_recovery_and_late_writer_cannot_recreate_it(self):
        with tempfile.TemporaryDirectory() as directory, SQLiteRepository(Path(directory) / 'state.db') as db:
            repo = ExecutionRepository(db)
            a, b = [db.create_conversation(workspace=directory) for _ in range(2)]
            ra, rb = [db.create_run(c.id, status='completed') for c in (a, b)]
            for c, r in ((a, ra), (b, rb)):
                repo.save_root(r.id, c.id, {'messages': ['private']}, [])
            self.assertTrue(db.delete_conversation(a.id))
            self.assertIsNone(repo.load_root(ra.id, a.id))
            self.assertIsNotNone(repo.load_root(rb.id, b.id))
            with self.assertRaises(ValueError):
                repo.save_root(ra.id, a.id, {'messages': ['revived']}, [])

    def test_open_existing_database_removes_only_orphan_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            with SQLiteRepository(path) as db:
                c = db.create_conversation(workspace=directory)
                r = db.create_run(c.id, status='completed')
                ExecutionRepository(db).save_root(r.id, c.id, {'messages': ['keep']}, [])
                db._connection.execute('INSERT INTO agent_root_recovery VALUES (?,?,?)', ('gone', 'gone', json.dumps({'messages': ['remove']})))
            with SQLiteRepository(path) as db:
                rows = db._connection.execute('SELECT run FROM agent_root_recovery').fetchall()
                self.assertEqual([row[0] for row in rows], [r.id])

    def test_live_native_execution_blocks_deletion_even_without_active_root_run(self):
        with tempfile.TemporaryDirectory() as directory, SQLiteRepository(Path(directory) / 'state.db') as db:
            c = db.create_conversation(workspace=directory)
            tl = db.ensure_conversation_task_list(c.id)
            t = db.create_task(tl.id, subject='a', description='a').task.id
            repo = ExecutionRepository(db)
            state = repo.create(c.id, directory, 'goal', [t])
            repo.change(state['id'], lambda r: r.update(status='running'))
            with self.assertRaises(StorageConflictError):
                db.delete_conversation(c.id)
            self.assertIsNotNone(db.get_conversation(c.id))
            repo.change(state['id'], lambda r: r.update(status='paused'))
            self.assertTrue(db.delete_conversation(c.id))
