"""Durable, versioned planning overlay. No repository writes or model calls."""
import hashlib
import json
from uuid import uuid4

from codeagent.events import utc_now_iso


PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS planning_sessions (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    mode TEXT NOT NULL DEFAULT 'off', target TEXT NOT NULL DEFAULT 'single',
    active_plan_id TEXT
);
CREATE TABLE IF NOT EXISTS planning_revisions (
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, status TEXT NOT NULL, title TEXT NOT NULL,
    markdown TEXT NOT NULL, payload_json TEXT NOT NULL, content_hash TEXT NOT NULL,
    run_id TEXT NOT NULL, read_only INTEGER NOT NULL DEFAULT 0,
    execution_run_id TEXT, team_run_id TEXT, error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(conversation_id, revision)
);
"""


def plan_hash(markdown, payload):
    return hashlib.sha256(json.dumps([markdown, payload], ensure_ascii=False,
                                    sort_keys=True).encode()).hexdigest()


def _plan(row):
    if row is None:
        return None
    result = dict(row)
    result['payload'] = json.loads(result.pop('payload_json'))
    result['read_only'] = bool(result['read_only'])
    return result


class PlanStorage:
    def planning_state(self, conversation_id):
        with self._lock:
            row = self._connection.execute('SELECT * FROM planning_sessions WHERE conversation_id=?',
                                           (conversation_id,)).fetchone()
            return dict(row) if row else {'conversation_id': conversation_id, 'mode': 'off',
                                         'target': 'single', 'active_plan_id': None}

    def set_planning_mode(self, conversation_id, mode, target='single'):
        if mode not in {'off', 'planning'} or target not in {'single', 'team'}:
            raise ValueError('Invalid planning mode or target')
        with self._transaction(immediate=True) as conn:
            conn.execute('INSERT INTO planning_sessions(conversation_id,mode,target) VALUES(?,?,?) '
                         'ON CONFLICT(conversation_id) DO UPDATE SET mode=excluded.mode,target=excluded.target',
                         (conversation_id, mode, target))
        return self.planning_state(conversation_id)

    def get_planning_revision(self, plan_id):
        from codeagent.web.storage import RecordNotFoundError
        with self._lock:
            row = self._connection.execute('SELECT * FROM planning_revisions WHERE id=?', (plan_id,)).fetchone()
        if row is None:
            raise RecordNotFoundError('Plan not found')
        return _plan(row)

    def list_planning_revisions(self, conversation_id):
        with self._lock:
            return [_plan(row) for row in self._connection.execute(
                'SELECT * FROM planning_revisions WHERE conversation_id=? ORDER BY revision DESC',
                (conversation_id,)).fetchall()]

    def save_planning_revision(self, conversation_id, *, run_id, title, markdown,
                               payload=None, submit=False, read_only=False):
        from codeagent.web.storage import StorageConflictError
        payload = payload or {}
        if not title.strip() or not markdown.strip():
            raise ValueError('方案标题和正文不能为空')
        if len(markdown) > 200000:
            raise ValueError('方案正文过长')
        now = utc_now_iso()
        with self._transaction(immediate=True) as conn:
            state = conn.execute('SELECT * FROM planning_sessions WHERE conversation_id=?', (conversation_id,)).fetchone()
            if not state or state['mode'] != 'planning':
                raise StorageConflictError('Plan Mode is not active')
            old = conn.execute('SELECT * FROM planning_revisions WHERE id=?', (state['active_plan_id'],)).fetchone()
            if old and old['status'] in {'approved', 'starting', 'blocked'}:
                raise StorageConflictError('Approved plan must be withdrawn before replanning')
            if old and old['status'] == 'draft':
                plan_id = old['id']
                conn.execute('UPDATE planning_revisions SET title=?,markdown=?,payload_json=?,content_hash=?, '
                             'status=?,read_only=?,updated_at=?,run_id=? WHERE id=?',
                             (title.strip(), markdown, json.dumps(payload, ensure_ascii=False), plan_hash(markdown, payload),
                              'submitted' if submit else 'draft', int(read_only), now, run_id, plan_id))
            else:
                if old and old['status'] == 'submitted':
                    conn.execute("UPDATE planning_revisions SET status='superseded',updated_at=? WHERE id=?", (now, old['id']))
                revision = conn.execute('SELECT COALESCE(MAX(revision),0)+1 FROM planning_revisions WHERE conversation_id=?',
                                        (conversation_id,)).fetchone()[0]
                plan_id = 'plan_' + uuid4().hex
                conn.execute('INSERT INTO planning_revisions(id,conversation_id,revision,status,title,markdown,payload_json,'
                             'content_hash,run_id,read_only,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                             (plan_id, conversation_id, revision, 'submitted' if submit else 'draft', title.strip(), markdown,
                              json.dumps(payload, ensure_ascii=False), plan_hash(markdown, payload), run_id, int(read_only), now, now))
            conn.execute('UPDATE planning_sessions SET active_plan_id=? WHERE conversation_id=?', (plan_id, conversation_id))
        return self.get_planning_revision(plan_id)

    def decide_planning_revision(self, plan_id, content_hash, decision):
        from codeagent.web.storage import StorageConflictError
        if decision not in {'approve', 'reject', 'withdraw', 'restore'}:
            raise ValueError('Invalid plan decision')
        with self._transaction(immediate=True) as conn:
            plan = self.get_planning_revision(plan_id)
            state = self.planning_state(plan['conversation_id'])
            if state['active_plan_id'] != plan_id or content_hash != plan['content_hash']:
                raise StorageConflictError('方案版本已经变化，请刷新预览后重试')
            if decision == 'approve' and plan['read_only']:
                raise StorageConflictError('只读保护下不能批准执行；请关闭保护后重新提交方案')
            target_status = {'approve': 'approved', 'reject': 'rejected', 'withdraw': 'withdrawn', 'restore': 'submitted'}[decision]
            if plan['status'] == target_status or (decision == 'approve' and plan['status'] in {'starting', 'started', 'blocked'}):
                return plan
            allowed = ({'rejected'} if decision == 'restore' else {'draft', 'submitted', 'rejected', 'approved', 'blocked'}
                       if decision == 'withdraw' else {'submitted'})
            if plan['status'] not in allowed:
                raise StorageConflictError('当前方案状态不允许此操作')
            conn.execute('UPDATE planning_revisions SET status=?,updated_at=? WHERE id=?', (target_status, utc_now_iso(), plan_id))
            if decision == 'withdraw':
                conn.execute("UPDATE planning_sessions SET mode='off' WHERE conversation_id=?", (plan['conversation_id'],))
        return self.get_planning_revision(plan_id)

    def update_plan_execution(self, plan_id, *, status, execution_run_id=None, team_run_id=None, error=None):
        if status not in {'starting', 'started', 'blocked'}:
            raise ValueError('Invalid plan execution status')
        with self._transaction(immediate=True) as conn:
            conn.execute('UPDATE planning_revisions SET status=?,execution_run_id=COALESCE(?,execution_run_id),'
                         'team_run_id=COALESCE(?,team_run_id),error=?,updated_at=? WHERE id=?',
                         (status, execution_run_id, team_run_id, error, utc_now_iso(), plan_id))
            if status == 'started':
                conn.execute("UPDATE planning_sessions SET mode='off' WHERE active_plan_id=?", (plan_id,))
        return self.get_planning_revision(plan_id)

    def pending_plan_launches(self):
        with self._lock:
            return [_plan(r) for r in self._connection.execute(
                "SELECT * FROM planning_revisions WHERE status IN ('approved','starting')").fetchall()]
