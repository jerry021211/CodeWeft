"""Execution metadata and dependency scheduling for the existing Task system."""
from __future__ import annotations

from copy import deepcopy
import re

PROFILES = {"research", "implement", "review", "verify"}


def paths(values):
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ValueError("Paths must be an array of repository-relative strings")
    result = []
    for raw in values:
        path = raw.replace("\\", "/").removeprefix("./").rstrip("/")
        if not path or path.startswith("/") or any(
            p in {"", ".", ".."} or ":" in p or p.rstrip(" .").casefold() == ".git"
            for p in path.split("/")
        ):
            raise ValueError(f"Invalid task path: {raw}")
        result.append(path)
    return result


def task_input(resource):
    task = resource.task
    config = validate_execution_metadata(task.metadata)
    return dict(id=task.id, subject=task.subject, objective=task.description,
                depends_on=list(task.blocked_by), **config)


def validate_execution_metadata(metadata):
    config = deepcopy(metadata.get('execution', {}))
    if not isinstance(config, dict):
        raise ValueError('metadata.execution must be an object')
    unknown = set(config) - {'profile', 'files', 'checks', 'resources', 'isolation'}
    if unknown:
        raise ValueError('Unknown execution fields: ' + ', '.join(sorted(unknown)) +
                         '. Source outputs belong in files; use project ignore rules for generated files.')
    checked = validate_plan([dict(id='validation', objective='validation', **config)])[0]
    return {k: checked[k] for k in ('profile', 'files', 'checks', 'resources', 'isolation')}


def validate_plan(tasks, *, read_only=False):
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 32:
        raise ValueError("Provide 1–32 independently verifiable tasks")
    items = deepcopy(tasks)
    ids = set()
    for task in items:
        if not isinstance(task, dict):
            raise ValueError("Every task must be an object")
        identifier = task.get("id", "")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier) or identifier in ids:
            raise ValueError("Task ids must be unique letters, numbers, underscores or hyphens")
        ids.add(identifier)
        if not isinstance(task.get("objective"), str) or not task["objective"].strip():
            raise ValueError(f"Task {identifier} requires an objective")
        task.setdefault("profile", "implement")
        if task["profile"] not in PROFILES:
            raise ValueError("Unknown worker profile")
        if read_only and task["profile"] not in {"research", "review"}:
            raise ValueError("Planning/read-only sessions may only delegate research or review")
        task["files"] = paths(task.get("files", []))
        task.setdefault("depends_on", [])
        task.setdefault("resources", [])
        for key in ("depends_on", "resources"):
            if not isinstance(task[key], list) or any(not isinstance(v, str) or not v for v in task[key]):
                raise ValueError(f"{key} must be an array of non-empty strings")
        task.setdefault("isolation", "auto")
        if task["isolation"] not in {"auto", "isolated"}:
            raise ValueError("isolation must be auto or isolated")
        task.setdefault("checks", [])
        from codeagent.runtime.validation import validate_step
        if not isinstance(task["checks"], list):
            raise ValueError("checks must be an array")
        for step in task["checks"]:
            validate_step(step)
        if task["profile"] in {"research", "review"} and task["checks"]:
            raise ValueError("Read-only tasks cannot run executable checks; use verify")
    remaining = {t["id"]: set(t["depends_on"]) for t in items}
    if any(deps - ids for deps in remaining.values()):
        raise ValueError("Task dependency does not exist; repair the plan before execution")
    while remaining:
        ready = {key for key, deps in remaining.items() if not deps}
        if not ready:
            raise ValueError("Task dependency cycle; repair the plan before execution")
        remaining = {key: deps - ready for key, deps in remaining.items() if key not in ready}
    return items


def guard_task_update(connection, task_list_id, task_id, changes):
    """Only execution-owned Tasks are protected; ordinary Tasks keep their behavior."""
    import json
    touched = {str(task_id)} | {str(v) for key in ('add_blocks', 'remove_blocks') for v in changes.get(key, [])}
    for row in connection.execute('SELECT data FROM agent_executions WHERE task_list_id=?', (task_list_id,)):
        run = json.loads(row[0])
        if run['status'] in {'completed', 'closed'}:
            continue
        for key in touched.intersection(run['task_ids']):
            record = run['records'][key]
            if set(changes) <= {'active_form'}:
                continue
            if run['status'] in {'running', 'verifying', 'delivering'} or record.get('inflight'):
                raise ValueError('Task is executing; stop execution before changing its inputs')
            if record['status'] == 'accepted':
                current = json.loads(connection.execute('SELECT metadata_json FROM tasks WHERE task_list_id=? AND id=?',
                                                        (task_list_id, key)).fetchone()[0])
                incoming = changes.get('metadata', {})
                old_config = validate_execution_metadata(current)
                new_config = incoming.get('execution')
                checks_only = (set(changes) == {'metadata'} and set(incoming) == {'execution'} and
                               isinstance(new_config, dict) and
                               {k: v for k, v in old_config.items() if k != 'checks'} ==
                               {k: v for k, v in validate_execution_metadata({'execution': new_config}).items() if k != 'checks'})
                if not checks_only:
                    raise ValueError('Verified task is immutable until delivery; create a follow-up Task for code changes')
            if key == str(task_id) and ('owner' in changes or changes.get('status', 'pending') != 'pending'):
                raise ValueError('Execution controls this Task status; use AgentResumeRun or AgentCloseRun')


def overlaps(left, right):
    """Conservative directory/glob overlap, including Windows case aliases."""
    def root(value):
        prefix = re.split(r"[?*\[]", value.casefold().replace("\\", "/"), 1)[0]
        return prefix.rstrip("/")
    for a in left:
        for b in right:
            x, y = root(a), root(b)
            if not x or not y or x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                return True
            # Wildcards may begin within a filename, not just at a separator.
            if any(c in a + b for c in "*?[") and (x.startswith(y) or y.startswith(x)):
                return True
    return False


def ready_wave(tasks, records, limit):
    ready = [t for t in tasks if records[t["id"]]["status"] == "pending"
             and all(records[d]["status"] == "accepted" for d in t["depends_on"])]
    selected = []
    for task in ready:
        write = task["profile"] in {"implement", "verify"}
        if any(set(task["resources"]) & set(other["resources"]) or
               (write and other["profile"] in {"implement", "verify"} and
                (not task["files"] or not other["files"] or overlaps(task["files"], other["files"])))
               for other in selected):
            continue
        selected.append(task)
        if len(selected) >= limit:
            break
    return selected
