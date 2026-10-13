"""Execution checkpoints for existing Tasks; never a second task database.

Task definitions and lifecycle changes use the application's Task tables. Execution
phases, checkpoints and publication journals live alongside them in the same DB.
"""
from copy import deepcopy
from datetime import datetime, UTC
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from . import ENGINE
from codeagent.tasks.execution import task_input, validate_plan


def fingerprint(task):
    return hashlib.sha256(json.dumps(task, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def code_fingerprint(task):
    return fingerprint({k: v for k, v in task.items() if k != 'checks'})


class ExecutionRepository:
    def __init__(self, service):
        self.service = service

    def inputs(self, task_list_id, task_ids, *, read_only=False, validate=True):
        if not isinstance(task_ids, list) or not task_ids or len(set(task_ids)) != len(task_ids):
            raise ValueError("Provide distinct existing taskIds from TaskList")
        tasks = []
        for key in task_ids:
            resource = self.service.get_task_resource(task_list_id, key)
            value = task_input(resource)
            # Completed prerequisites outside this execution remain canonical Tasks.
            value['depends_on'] = [dep for dep in value['depends_on'] if dep in task_ids or
                self.service.get_task_resource(task_list_id, dep).task.status.value != 'completed']
            tasks.append(value)
        return validate_plan(tasks, read_only=read_only) if validate else tasks

    def create(self, conversation, workspace, objective, task_ids, *, read_only=False, mode="plan"):
        if mode not in {'single', 'batch', 'plan'}:
            raise ValueError('Unknown execution mode')
        if mode == 'single' and len(task_ids) != 1 or mode == 'batch' and len(task_ids) > 5:
            raise ValueError('Task count exceeds the selected execution mode')
        task_list = self.service.ensure_conversation_task_list(conversation)
        with self.service._transaction(immediate=True) as db:
            tasks = self.inputs(task_list.id, task_ids, read_only=read_only)
            if mode != 'plan' and any(t['isolation'] == 'isolated' for t in tasks):
                raise ValueError('Use execute_plan for explicitly isolated workspaces')
            if any(r['status'] not in {'completed', 'closed'} for r in self.list(conversation, workspace)):
                raise ValueError("Continue or close the existing execution before starting another")
            for row in db.execute('SELECT data FROM agent_executions WHERE task_list_id=?', (task_list.id,)):
                existing = json.loads(row[0])
                if existing['status'] not in {'completed', 'closed'} and set(existing['task_ids']) & set(task_ids):
                    raise ValueError('A selected Task already belongs to an unfinished execution')
            for task in tasks:
                row = self.service.get_task_resource(task_list.id, task['id']).task
                if row.status.value in {'completed', 'in_progress'} or row.owner:
                    raise ValueError(f"Task #{row.id} is already completed or owned; use a pending Task")
            value = dict(id='collab_' + uuid4().hex, engine=ENGINE, version=2,
                         conversation=conversation, task_list_id=task_list.id,
                         workspace=str(Path(workspace).resolve()), objective=objective,
                         status='pending', read_only=read_only, task_ids=task_ids, mode=mode,
                         epoch=0, revision=0, records={t['id']: dict(status='pending', attempts=0,
                         summary='', input_hash=fingerprint(t), code_hash=code_fingerprint(t)) for t in tasks},
                         checkpoints={}, workspaces={}, issue=None, delivery=None)
            db.execute('INSERT INTO agent_executions VALUES (?,?,?,?,?)',
                       (value['id'], conversation, task_list.id, value['workspace'], json.dumps(value)))
        return self.get(value['id'])

    def get(self, identifier, *, conversation=None, workspace=None):
        with self.service._lock:
            row = self.service._connection.execute('SELECT data FROM agent_executions WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise ValueError('Unknown agent execution')
            value = json.loads(row[0])
            if ((conversation is not None and value['conversation'] != conversation) or
                    (workspace is not None and value['workspace'] != str(Path(workspace).resolve()))):
                raise ValueError('Execution does not belong to this conversation/workspace')
            # This is a read projection, never persisted as another task definition.
            value['tasks'] = self.inputs(value['task_list_id'], value['task_ids'], read_only=value['read_only'], validate=False)
            return value

    def list(self, conversation, workspace):
        with self.service._lock:
            rows = self.service._connection.execute(
                'SELECT id FROM agent_executions WHERE conversation=? AND workspace=? ORDER BY rowid',
                (conversation, str(Path(workspace).resolve()))).fetchall()
            return [self.get(row[0]) for row in rows]

    def append_tasks(self, identifier, task_ids):
        """Attach existing follow-up Tasks without changing accepted task definitions."""
        with self.service._transaction(immediate=True) as db:
            value = self.get(identifier)
            if value['status'] not in {'paused', 'blocked', 'pending'} or value.get('delivery'):
                raise ValueError('Stop execution and recover pending delivery before adding Tasks')
            if any(r.get('inflight') for r in value['records'].values()) or value.get('runtime_unknown'):
                raise ValueError('Recover interrupted operations before adding Tasks')
            added = [key for key in task_ids if key not in value['task_ids']]
            all_ids = value['task_ids'] + added
            tasks = self.inputs(value['task_list_id'], all_ids, read_only=value['read_only'])
            for task in tasks:
                if task['id'] not in added:
                    continue
                resource = self.service.get_task_resource(value['task_list_id'], task['id'])
                if resource.task.status.value != 'pending' or resource.task.owner:
                    raise ValueError('Additional Tasks must be pending and unowned')
                for row in db.execute('SELECT id,data FROM agent_executions WHERE task_list_id=? AND id<>?',
                                      (value['task_list_id'], identifier)):
                    other = json.loads(row['data'])
                    if other['status'] not in {'closed', 'completed'} and task['id'] in other['task_ids']:
                        raise ValueError('Additional Task belongs to another execution')
                value['records'][task['id']] = dict(status='pending', attempts=0, summary='',
                    input_hash=fingerprint(task), code_hash=code_fingerprint(task))
            value['task_ids'] = all_ids
            value['revision'] += 1
            value.pop('tasks')
            db.execute('UPDATE agent_executions SET data=? WHERE id=?', (json.dumps(value), identifier))

    def change(self, identifier, update, *, epoch=None):
        with self.service._transaction(immediate=True) as db:
            value = self.get(identifier)
            if epoch is not None and epoch != value['epoch']:
                raise ValueError('Stale execution')
            previous = deepcopy(value)
            update(value)
            if value['tasks'] != previous['tasks']:
                raise ValueError('Change tasks through TaskUpdate, not the execution journal')
            value['revision'] += 1
            value['updated_at'] = datetime.now(UTC).isoformat()
            self._project_progress(db, previous, value)
            stored = {key: item for key, item in value.items() if key != 'tasks'}
            db.execute('UPDATE agent_executions SET data=? WHERE id=?', (json.dumps(stored), identifier))
        self.service._notify_activity()
        return value

    def _project_progress(self, db, before, value):
        """Task status + execution phase commit together, including failures/cancellation."""
        for key, record in value['records'].items():
            phase = record['status']
            if value['status'] == 'closed' and phase != 'accepted':
                record['status'] = phase = 'closed'
            elif value['status'] in {'paused', 'blocked'} and phase in {'running', 'reported', 'pending'}:
                record['status'] = phase = 'paused' if value['status'] != 'closed' else 'closed'
            old = before['records'].get(key, {})
            if all(record.get(k) == old.get(k) for k in ('status', 'reason', 'summary')):
                continue
            status = {'accepted': 'completed', 'running': 'in_progress', 'reported': 'in_progress',
                      'failed': 'failed', 'blocked': 'failed', 'closed': 'cancelled'}.get(phase, 'pending')
            owner = value['id'] + '_' + key if status == 'in_progress' else None
            active = {'running': '成员执行中', 'reported': '成果已返回，等待验证', 'accepted': '成员成果已接收',
                      'paused': '执行已暂停，现场已保留', 'pending': '等待调度', 'failed': '需要修复',
                      'blocked': '需要主 Agent 处理', 'closed': '执行已结束'}.get(phase, phase)
            db.execute('UPDATE tasks SET status=?, owner=?, active_form=?, revision=revision+1, updated_at=? '
                       'WHERE task_list_id=? AND id=?',
                       (status, owner, active, value['updated_at'], value['task_list_id'], key))
            db.execute('UPDATE task_lists SET revision=revision+1, updated_at=? WHERE id=?',
                       (value['updated_at'], value['task_list_id']))
            self.service._insert_task_activity(db, task_list_id=value['task_list_id'], task_id=key,
                event_type='completed' if status == 'completed' else 'updated',
                conversation_id=value['conversation'], run_id=value.get('root_run_id'), agent_id=owner,
                payload=dict(execution_id=value['id'], phase=phase, status=status,
                             reason=record.get('reason', ''), summary=record.get('summary', '')),
                created_at=value['updated_at'])

    @staticmethod
    def public(value):
        return {key: value.get(key) for key in ('id', 'engine', 'objective', 'status', 'revision',
                'task_list_id', 'mode', 'tasks', 'records', 'issue', 'checks', 'updated_at')}

    def save_root(self, run, conversation, checkpoint, operations):
        with self.service._transaction(immediate=True) as db:
            db.execute('INSERT OR REPLACE INTO agent_root_recovery VALUES (?,?,?)',
                       (run, conversation, json.dumps(dict(checkpoint=checkpoint, operations=operations))))

    def load_root(self, run, conversation):
        with self.service._lock:
            row = self.service._connection.execute(
                'SELECT data FROM agent_root_recovery WHERE run=? AND conversation=?', (run, conversation)).fetchone()
            return json.loads(row[0]) if row else None
