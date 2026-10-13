"""Native collaboration behavioral tests; no external models or user repositories."""
from pathlib import Path
import tempfile
import unittest

from codeagent.tasks.execution import validate_plan, ready_wave, overlaps
from codeagent.multiagent.executions import ExecutionRepository
from codeagent.web.storage import SQLiteRepository


class NativePlanTests(unittest.TestCase):
    def test_invalid_graph_is_rejected_before_dispatch(self):
        for tasks in ([dict(id="a", objective="a", depends_on=["missing"])],
                      [dict(id="a", objective="a", depends_on=["b"]), dict(id="b", objective="b", depends_on=["a"])]):
            with self.assertRaises(ValueError):
                validate_plan(tasks)

    def test_capacity_never_discards_deferred_work(self):
        tasks = validate_plan([dict(id=n, objective=n, files=[n + "/**"]) for n in "abc"])
        records = {n: {"status": "pending"} for n in "abc"}
        self.assertEqual([t["id"] for t in ready_wave(tasks, records, 1)], ["a"])
        records["a"]["status"] = "accepted"
        self.assertEqual([t["id"] for t in ready_wave(tasks, records, 2)], ["b", "c"])

    def test_windows_directories_and_globs_conflict(self):
        self.assertTrue(overlaps(["Src/**"], ["src/a.py"]))
        self.assertTrue(overlaps(["src/a*.py"], ["src/abc.py"]))
        self.assertFalse(overlaps(["web/**"], ["server/**"]))

    def test_failed_dependency_never_unlocks_task(self):
        tasks = validate_plan([dict(id="a", objective="a"), dict(id="b", objective="b", depends_on=["a"])])
        self.assertEqual(ready_wave(tasks, {"a": {"status": "failed"}, "b": {"status": "pending"}}, 3), [])

    def test_readonly_cannot_delegate_writes_or_checks(self):
        with self.assertRaises(ValueError):
            validate_plan([dict(id="a", objective="a")], read_only=True)
        with self.assertRaises(ValueError):
            validate_plan([dict(id="a", objective="a", profile="review", checks=["python --version"])])

    def test_stale_execution_and_cross_conversation_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            service = SQLiteRepository(Path(directory) / 'web.db', recover_incomplete=False)
            self.addCleanup(service.close)
            conv = service.create_conversation(workspace=directory)
            task_list = service.ensure_conversation_task_list(conv.id)
            task = service.create_task(task_list.id, subject='a', description='a')
            repo = ExecutionRepository(service)
            record = repo.create(conv.id, directory, 'goal', [task.task.id])
            repo.change(record["id"], lambda r: r.update(epoch=1))
            with self.assertRaises(ValueError):
                repo.change(record["id"], lambda r: r.update(status="completed"), epoch=0)
            with self.assertRaises(ValueError):
                repo.get(record["id"], conversation="other")
            self.assertEqual(ExecutionRepository(service).get(record["id"])["epoch"], 1)
            service.close()


if __name__ == "__main__":
    unittest.main()
