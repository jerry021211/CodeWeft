from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codeagent.web.storage import SQLiteRepository, StorageConflictError, RecordNotFoundError


class MessageActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.temp.name) / "state.db", recover_incomplete=False)
        self.source = self.repo.create_conversation(title="原对话", workspace=self.temp.name)
        self.history = []

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def turn(self, prompt, answer, **metadata):
        run = self.repo.create_run(self.source.id, status="completed", metadata=metadata)
        user = self.repo.create_message(self.source.id, role="user", content=prompt, run_id=run.id)
        reply = self.repo.create_message(self.source.id, role="assistant", content=answer, run_id=run.id)
        self.history += [{"role": "user", "content": prompt}, {"role": "assistant", "content": answer}]
        self.repo.save_checkpoint(run.id, messages=self.history, todos=[{"content": "old task"}],
                                  context={"summary_text": "earlier context", "runtime_reminders": {"pending": True}}, metadata={"native_collaboration": True,
                                  "tool_history_version": 1, "execution_guard": {"old": True}})
        return run, user, reply

    def test_fork_has_exact_history_independent_ids_and_survives_source_deletion(self):
        run, _, reply = self.turn("first", "answer")
        history = list(self.history)
        self.turn("future", "must not appear")
        fork, retry = self.repo.fork_conversation(self.source.id, reply.id)
        self.assertIsNone(retry)
        self.assertEqual(fork.workspace, self.source.workspace)
        self.assertNotEqual(fork.active_task_list_id, self.source.active_task_list_id)
        copied = self.repo.list_messages(fork.id)
        self.assertEqual([m.content for m in copied], ["first", "answer"])
        self.assertNotEqual(copied[-1].id, reply.id)
        self.assertNotEqual(copied[-1].run_id, run.id)
        checkpoint = self.repo.get_latest_checkpoint(fork.id)
        self.assertEqual(checkpoint.messages, history)
        self.assertEqual(checkpoint.todos, [])
        self.assertEqual(checkpoint.context["summary_text"], "earlier context")
        self.assertEqual(checkpoint.context["runtime_reminders"], {})
        self.assertNotIn("execution_guard", checkpoint.metadata)
        self.assertTrue(checkpoint.metadata["native_collaboration"])
        self.assertEqual(len(self.repo.list_messages(self.source.id)), 4)
        self.repo.delete_conversation(self.source.id)
        self.assertEqual(self.repo.get_latest_checkpoint(fork.id).messages, history)
        again, _ = self.repo.fork_conversation(fork.id, copied[-1].id)
        self.assertEqual(self.repo.get_latest_checkpoint(again.id).messages, history)

    def test_regenerate_uses_previous_context_and_original_options(self):
        self.turn("first", "answer")
        previous = list(self.history)
        _, _, reply = self.turn("retry this", "exclude this", read_only=True, web_search_enabled=True,
                                reasoning_effort="high", plan_mode=True)
        self.turn("future", "also excluded")
        fork, retry = self.repo.fork_conversation(self.source.id, reply.id, regenerate=True)
        self.assertEqual([m.content for m in self.repo.list_messages(fork.id)], ["first", "answer"])
        self.assertEqual(self.repo.get_latest_checkpoint(fork.id).messages, previous)
        self.assertEqual(retry, {"content": "retry this", "readOnly": True, "webSearch": True,
                                  "reasoningEffort": "high", "planMode": True})
        self.assertEqual(len(self.repo.list_messages(self.source.id)), 6)

    def test_first_turn_regenerate_starts_without_history(self):
        _, _, reply = self.turn("original question", "old answer")
        fork, retry = self.repo.fork_conversation(self.source.id, reply.id, regenerate=True)
        self.assertEqual(self.repo.list_messages(fork.id), [])
        self.assertIsNone(self.repo.get_latest_checkpoint(fork.id))
        self.assertEqual(retry["content"], "original question")

    def test_attachments_missing_checkpoints_and_wrong_messages_do_not_create_partial_forks(self):
        run, user, reply = self.turn("question", "answer")
        self.repo.update_message(user.id, metadata={"attachments": [{"name": "image.png"}]})
        with self.assertRaisesRegex(StorageConflictError, "附件"):
            self.repo.fork_conversation(self.source.id, reply.id, regenerate=True)
        with self.assertRaises(RecordNotFoundError):
            self.repo.fork_conversation(self.source.id, "wrong-id")
        with self.assertRaises(StorageConflictError):
            self.repo.fork_conversation(self.source.id, user.id)
        with patch.object(self.repo, "get_checkpoint_for_run", return_value=None):
            with self.assertRaisesRegex(StorageConflictError, "快照"):
                self.repo.fork_conversation(self.source.id, reply.id)
        with patch.object(self.repo, "_upsert_checkpoint", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                self.repo.fork_conversation(self.source.id, reply.id)
        self.assertEqual(len(self.repo.list_conversations()), 1)

    def test_tool_history_is_preserved_without_restoring_execution_permissions(self):
        run, _, reply = self.turn("question", "answer")
        history = [{"role": "user", "content": "question"},
                   {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read", "input": {}}]},
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "file text"}]},
                   {"role": "assistant", "content": "answer"}]
        self.repo.save_checkpoint(run.id, messages=history, todos=[], context={},
                                  metadata={"tool_history_version": 1, "execution_guard": {"approved": True}})
        fork, _ = self.repo.fork_conversation(self.source.id, reply.id)
        self.assertEqual(self.repo.get_latest_checkpoint(fork.id).messages, history)
        self.assertNotIn("execution_guard", self.repo.get_latest_checkpoint(fork.id).metadata)

    def test_running_team_and_nonfinal_replies_cannot_be_forked(self):
        run, _, reply = self.turn("question", "answer", requested_mode="team")
        with self.assertRaisesRegex(StorageConflictError, "团队"):
            self.repo.fork_conversation(self.source.id, reply.id)
        run, _, reply = self.turn("next", "intermediate")
        self.repo.create_message(self.source.id, role="assistant", content="last", run_id=run.id)
        with self.assertRaisesRegex(StorageConflictError, "最后"):
            self.repo.fork_conversation(self.source.id, reply.id)
        active = self.repo.create_run(self.source.id)
        reply = self.repo.create_message(self.source.id, role="assistant", content="stream", run_id=active.id)
        with self.assertRaisesRegex(StorageConflictError, "已完成"):
            self.repo.fork_conversation(self.source.id, reply.id)

    def test_next_run_receives_forked_checkpoint_and_keeps_source_unchanged(self):
        from tests.test_web_scheduler import _FakeFactory
        from codeagent.web.scheduler import RunScheduler

        self.turn("first", "first answer")
        expected = list(self.history)
        _, _, reply = self.turn("second", "old answer", read_only=True)
        fork, retry = self.repo.fork_conversation(self.source.id, reply.id, regenerate=True)
        received = []

        class Factory(_FakeFactory):
            def create(self, **kwargs):
                checkpoint = kwargs["checkpoint"]
                received.append(checkpoint)
                agent = super().create(**kwargs)
                agent.messages = list(checkpoint.messages) if checkpoint else []
                return agent

        factory = Factory()
        scheduler = RunScheduler(self.repo, factory)
        try:
            run = scheduler.submit(fork.id, retry["content"], read_only=retry["readOnly"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = self.repo.get_run(run.id)
                if current.status in {"completed", "failed"}:
                    break
                time.sleep(0.01)
            self.assertEqual(current.status, "completed", current.error)
            self.assertEqual(received[0].messages, expected)
            self.assertEqual(factory.read_only_choices, [True])
            self.assertEqual(self.repo.get_latest_checkpoint(fork.id).messages,
                             expected + [{"role": "user", "content": "second"}, {"role": "assistant", "content": "答复:second"}])
            self.assertEqual([m.content for m in self.repo.list_messages(self.source.id)],
                             ["first", "first answer", "second", "old answer"])
        finally:
            scheduler.stop()


if __name__ == "__main__":
    unittest.main()
