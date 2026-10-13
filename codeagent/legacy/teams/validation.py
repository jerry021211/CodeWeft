"""Validation plan schema retained for historical Team plans."""
from codeagent.runtime.validation import validate_step


def validate_checks(plan):
    checks = plan.get("validation_checks", [])
    if not isinstance(checks, list):
        raise ValueError("validation_checks must be an array")
    task_ids = {str(t["task_id"]) for t in plan.get("tasks", []) if isinstance(t, dict) and "task_id" in t}
    seen = set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("id"), str) or not check["id"].strip() or check["id"] in seen:
            raise ValueError("Validation checks require unique non-empty ids")
        seen.add(check["id"])
        if check.get("stage") not in {"integration", "delivery"}:
            raise ValueError("Validation stage must be integration or delivery")
        requires = check.get("requires_tasks", [])
        if not isinstance(requires, list) or any(str(t) not in task_ids for t in requires):
            raise ValueError("Validation prerequisites must reference planned task ids")
        steps = check.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("Validation checks require non-empty steps")
        for step in steps:
            validate_step(step)
    return checks
