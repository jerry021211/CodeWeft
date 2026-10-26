from pathlib import Path
import tempfile
from unittest import TestCase
from codeagent.web.storage import SQLiteRepository
from codeagent.multiagent.executions import ExecutionRepository


class NativeUsageTests(TestCase):
    def test_member_usage_spans_resumes_without_root_other_members_or_other_conversations(self):
        with tempfile.TemporaryDirectory() as directory, SQLiteRepository(Path(directory) / 'state.db') as db:
            c = db.create_conversation(workspace=directory)
            tl = db.ensure_conversation_task_list(c.id)
            keys = [db.create_task(tl.id, subject=name, description=name).task.id for name in ('a', 'b')]
            repo = ExecutionRepository(db)
            record = repo.create(c.id, directory, 'goal', keys)
            a, b = [record['id'] + '_' + key for key in keys]
            def call(conv, agent, tokens, available=True):
                run = db.create_run(conv.id, status='completed')
                db.create_model_call(run.id, agent_id=agent, model='test', status='completed',
                    usage=dict(input_tokens=tokens, output_tokens=2, cache_read_input_tokens=10, available=True) if available else None)
            call(c, a, 20)
            call(c, a, 30)  # Same member in a resumed root run.
            call(c, b, 80)
            call(c, 'agent_root', 900)
            call(c, a, 0, available=False)
            other = db.create_conversation(workspace=directory)
            call(other, a, 999)
            usage = repo.usage(record, keys[0])
            self.assertEqual(usage['model_calls'], 3)
            self.assertEqual(usage['total_tokens'], 74)
            self.assertEqual(usage['unavailable_calls'], 1)
            self.assertEqual(usage, repo.usage(record, keys[0]))
            self.assertEqual(repo.usage(record)['model_calls'], 4)
            self.assertEqual(db.aggregate_usage(conversation_id=c.id)['model_calls'], 5)
