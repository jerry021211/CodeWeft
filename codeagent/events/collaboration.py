"""Small UI control snapshots. Long reports remain in the execution repository."""


def compact_snapshot(value):
    def text(item, limit):
        return str(item or '')[:limit]

    tasks = value.get('tasks') or []
    records = value.get('records') or {}
    result = {k: value.get(k) for k in ('revision', 'updated_at')}
    for key in ('id', 'engine', 'task_list_id', 'mode', 'status'):
        result[key] = text(value.get(key), 128)
    result['objective'] = text(value.get('objective'), 256)
    result['details_available'] = True
    result['tasks'], result['records'] = [], {}
    for task in tasks[:32]:
        key = text(task.get('id'), 64)
        result['tasks'].append(dict(id=key, subject=text(task.get('subject'), 120),
            profile=text(task.get('profile'), 32),
            depends_on=[text(dep, 64) for dep in task.get('depends_on', [])[:32]]))
        record = records.get(key, {})
        result['records'][key] = dict(status=text(record.get('status'), 32),
            attempts=record.get('attempts', 0), reason=text(record.get('reason'), 160))
        if record.get('member_id'):
            result['records'][key]['member_id'] = text(record['member_id'], 128)
    issue = value.get('issue')
    result['issue'] = (dict(owner=text(issue.get('owner'), 32), reason=text(issue.get('reason'), 256))
                       if isinstance(issue, dict) else None)
    return result
