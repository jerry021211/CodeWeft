"""One-way import of native_v1 data. Never execute or modify the old database."""
from datetime import datetime, UTC
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from . import ENGINE
from .executions import fingerprint, code_fingerprint
from codeagent.tasks.execution import task_input


def import_legacy(service, path, conversation, workspace):
    path = Path(path)
    if not path.is_file():
        return
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as old:
        rows = old.execute('SELECT data FROM collaborations WHERE conversation=? AND workspace=?',
                           (conversation, str(Path(workspace).resolve()))).fetchall()
        roots = old.execute('SELECT run, data FROM root_recovery WHERE conversation=?', (conversation,)).fetchall()
    task_list = service.ensure_conversation_task_list(conversation)
    now = datetime.now(UTC).isoformat()
    with service._transaction(immediate=True) as db:
        for raw, in rows:
            run = json.loads(raw)
            if db.execute('SELECT 1 FROM agent_executions WHERE id=?', (run['id'],)).fetchone():
                continue
            tasks = run.pop('tasks')
            start = db.execute('SELECT next_task_number FROM task_lists WHERE id=?', (task_list.id,)).fetchone()[0]
            mapping = {task['id']: str(start + n) for n, task in enumerate(tasks)}
            if run['status'] in {'running', 'verifying', 'delivering'}:
                run.update(status='paused', issue=dict(owner='user', reason='旧执行已迁入任务系统，现场已保留；等待继续'))
            for task in tasks:
                key = mapping[task['id']]
                record = run['records'][task['id']]
                record['member_id'] = run['id'] + '_' + task['id']
                if record['status'] in {'running', 'reported'}:
                    record.update(status='paused', reason='旧执行现场已保留')
                config = {k: task[k] for k in ('profile', 'files', 'checks', 'resources', 'isolation') if k in task}
                status = 'completed' if record['status'] == 'accepted' else 'pending'
                if run['status'] == 'closed' and status != 'completed':
                    status = 'cancelled'
                db.execute('INSERT INTO tasks(task_list_id,id,subject,description,status,metadata_json,created_at,updated_at) '
                           'VALUES (?,?,?,?,?,?,?,?)', (task_list.id, key, task.get('subject') or task['id'], task['objective'],
                           status, json.dumps({'execution': config}), now, now))
                service._insert_task_activity(db, task_list_id=task_list.id, task_id=key,
                    event_type='created', conversation_id=conversation, run_id=None, agent_id=None,
                    payload={'migration': 'native_v1', 'execution_id': run['id']}, created_at=now)
            for task in tasks:
                for dep in task.get('depends_on', []):
                    db.execute('INSERT INTO task_dependencies VALUES (?,?,?)',
                               (task_list.id, mapping[dep], mapping[task['id']]))
            db.execute('UPDATE task_lists SET next_task_number=?, revision=revision+1, updated_at=? WHERE id=?',
                       (start + len(tasks), now, task_list.id))
            run['records'] = {mapping[k]: v for k, v in run['records'].items() if k in mapping}
            run['checkpoints'] = {mapping[k]: v for k, v in run.get('checkpoints', {}).items() if k in mapping}
            renamed_paths = {'task:' + old_key + suffix: 'task:' + new_key + suffix
                             for old_key, new_key in mapping.items() for suffix in ('', ':base')}
            run['workspaces'] = {renamed_paths.get(key, key): value
                                 for key, value in run['workspaces'].items()}
            for check in run.get('checks', []):
                check['task_id'] = mapping.get(check.get('task_id'), check.get('task_id'))
            run.update(version=2, engine=ENGINE, mode='plan', task_list_id=task_list.id, task_ids=list(mapping.values()), revision=run['revision'] + 1)
            for key, record in run['records'].items():
                task = task_input(service.get_task_resource(task_list.id, key))
                record['input_hash'] = fingerprint(task)
                record['code_hash'] = code_fingerprint(task)
            db.execute('INSERT INTO agent_executions VALUES (?,?,?,?,?)',
                       (run['id'], conversation, task_list.id, run['workspace'], json.dumps(run)))
        for run_id, data in roots:
            db.execute('INSERT OR IGNORE INTO agent_root_recovery VALUES (?,?,?)', (run_id, conversation, data))
    service._notify_activity()
