import unittest
from codeagent.legacy.teams.validation import validate_checks


class TeamValidationTests(unittest.TestCase):
    def test_plan_checks_reject_unknown_prerequisites_and_bad_steps(self):
        for check in [
            {"id": "x", "stage": "integration", "requires_tasks": ["absent"], "steps": ["git status"]},
            {"id": "x", "stage": "integration", "steps": []},
            {"id": "x", "stage": "delivery", "steps": [{"argv": ["git"], "cwd": "C:/outside"}]},
        ]:
            with self.assertRaises(ValueError):
                validate_checks({"tasks": [{"task_id": "1"}], "validation_checks": [check]})
