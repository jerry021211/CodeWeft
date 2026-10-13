"""Current-version status and bounded advice. Never executes or authorizes tools.

All methods run on the parent Agent's observation hooks. The evidence ledger is
the only source of tool results; this module stores baselines and delivery IDs.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re

from codeagent.cvm.snapshot import capture, digest, same_version
from codeagent.cvm.verification import VerificationDefinition
from codeagent.messages import extract_text

PROJECT_CONFIG = '.codeagent/cvm-verification.json'
LABELS = {
    'missing': '尚无当前改动的有效验证记录', 'failed': '检查失败',
    'incomplete': '检查受阻或未完成', 'stale': '旧结果已过期',
    'passed': '指定检查通过', 'unknown': '范围或结果未知',
}


def restore_supervision(payload):
    """Corrupt/future policy state disables CVM through the existing restore path."""
    body = {key: value for key, value in payload.items() if key != 'content_sha256'}
    if (body.get('schema_version') != 1 or payload.get('content_sha256') != digest(body)
            or not isinstance(body.get('task_id'), str)
            or not isinstance(body.get('baselines'), dict)
            or not isinstance(body.get('delivered'), dict)
            or len(body['delivered']) > 512
            or type(body.get('batches')) is not int
            or type(body.get('suppressed')) is not bool
            or not isinstance(body.get('waiting'), dict)):
        raise ValueError('invalid_supervision_state')
    if (any(not isinstance(key, str) or type(value) is not int or value < 0 for key, value in body['waiting'].items())
            or any(not isinstance(s, dict) or s.get('quality') not in {'complete', 'partial'}
                   or not isinstance(s.get('files'), dict) for s in body['baselines'].values())
            or any(not isinstance(d, dict) or not isinstance(d.get('definition_id'), str)
                   or type(d.get('after')) is not int or d['after'] < 0
                   or not isinstance(d.get('seen'), list) or len(d['seen']) > 5000
                   or any(not isinstance(item, str) for item in d['seen']) for d in body['delivered'].values())):
        raise ValueError('invalid_supervision_details')
    return deepcopy(payload)


def python_changed(before, after):
    if not before or before.get('quality') != 'complete' or after.get('quality') != 'complete':
        return False
    left, right = before['files'], after['files']
    return any(path.endswith('.py') and left.get(path) != right.get(path) for path in left.keys() | right.keys())


class VerificationSupervisor:
    agent_local = True

    def __init__(self, runtime, *, emit, can_advise):
        self.runtime, self.emit, self.can_advise = runtime, emit, can_advise
        self.pending = None
        self.last_status = None
        self.config_fingerprint = None
        self.config_error = False
        self.candidate_ids = set()

    @property
    def enabled(self):
        return (self.runtime.recording and self.runtime.config.verification != 'off'
                and self.runtime.source_kind == 'user' and self.runtime.current is not None)

    @property
    def state(self):
        return self.runtime.supervision

    def save(self):
        self.state['content_sha256'] = digest({k: v for k, v in self.state.items() if k != 'content_sha256'})

    def begin(self, prompt):
        self.pending, self.last_status = None, None
        self.candidate_ids.clear()
        if not self.enabled:
            return
        if self.state.get('task_id') != self.runtime.current.task_id:
            self.runtime.supervision = dict(schema_version=1, task_id=self.runtime.current.task_id,
                                           baselines={}, delivered={}, batches=0, waiting={}, suppressed=False)
        # Conservative veto, never an authorization parser. Pending/legacy goals
        # are separately excluded. A veto survives 'continue' and checkpoint restore.
        if re.search(r'不要.*(?:测试|检查|执行)|禁止|不(?:运行|执行|测试)|只.*(?:交接|文档|解释|回答)|暂停|停止|'
                     r"don.t (?:run|test)|do not|no tests|handoff|pause|stop", prompt, re.I):
            self.state['suppressed'] = True
        self.load_project_config()
        for key, definition in self.definitions().items():
            if key not in self.state['baselines']:
                self.state['baselines'][key] = self.snapshot(definition)
        self.save()
        self.refresh()

    def definitions(self):
        return {key: value for key, value in self.runtime.evidence.definitions.items()
                if value['task_id'] == self.runtime.current.task_id
                and (value['source_kind'] != 'project_config' or key in getattr(self, 'project_ids', set()))}

    def snapshot(self, definition):
        return capture(self.runtime.workspace, definition['inputs'],
                       cwd=str(Path(self.runtime.workspace) / definition['cwd']))

    def load_project_config(self):
        self.config_error, self.config_fingerprint = False, None
        self.project_ids = set()
        self.runtime.evidence.active_project_definitions = self.project_ids
        root = Path(self.runtime.workspace)
        snapshot = capture(root, [PROJECT_CONFIG], max_bytes=32768)
        self.config_fingerprint = snapshot['fingerprint']
        path = root / PROJECT_CONFIG
        if snapshot['quality'] != 'complete':
            self.config_error = True
            return
        if not path.exists():
            return
        try:
            config = json.loads(path.read_text(encoding='utf-8'))
            if set(config) != {'version', 'checks'} or config['version'] != 1 or not 1 <= len(config['checks']) <= 16:
                raise ValueError('invalid_project_config')
            definitions = []
            for item in config['checks']:
                if (set(item) - {'command', 'cwd', 'inputs'} or len(item['command']) > 512
                        or not isinstance(item['inputs'], list) or not item['inputs'] or len(item['inputs']) > 64):
                    raise ValueError('invalid_project_check')
                # Include the configuration itself in version validity. No model
                # tool can register definitions; reload only at real user input.
                definitions.append(VerificationDefinition(
                    **{**item, 'inputs': (*item['inputs'], str(path))},
                    source_kind='project_config', source_ref=PROJECT_CONFIG,
                    task_id=self.runtime.current.task_id))
            # Validate all paths before replacing the previous project set.
            from codeagent.cvm.evidence import EvidenceLedger
            validation = EvidenceLedger(root)
            commands = set()
            for definition in definitions:
                validation.register(definition)
                identity = (definition.command, os.path.normcase(str((root / definition.cwd).resolve())))
                if identity in commands:
                    raise ValueError('duplicate_project_check')
                commands.add(identity)
            ledger = self.runtime.evidence
            for key, old in list(ledger.definitions.items()):
                if old['source_kind'] == 'project_config' and old['task_id'] == self.runtime.current.task_id:
                    # Retain historical definitions because evidence references them.
                    if key not in validation.definitions:
                        self.state['baselines'].pop(key, None)
            for definition in definitions:
                ledger.register(definition)
            self.project_ids = set(validation.definitions)
            ledger.active_project_definitions = self.project_ids
        except (ValueError, TypeError, KeyError, OSError):
            self.config_error = True

    def refresh(self):
        if not self.enabled:
            return []
        ledger = self.runtime.evidence
        definitions = self.definitions()
        snapshots, cache = {}, {}
        for key, definition in definitions.items():
            cwd = os.path.normcase(str((Path(self.runtime.workspace) / definition['cwd']).resolve()))
            cache_key = (cwd, tuple(definition['inputs']))
            if cache_key not in cache:
                cache[cache_key] = self.snapshot(definition)
            snapshots[key] = cache[cache_key]
        ledger.refresh(cache)
        config_now = capture(self.runtime.workspace, [PROJECT_CONFIG], max_bytes=32768)
        config_changed = config_now['quality'] != 'complete' or config_now['fingerprint'] != self.config_fingerprint
        records = [r for r in ledger.verifications if r['task_id'] == self.runtime.current.task_id]
        rows = []
        for key, definition in definitions.items():
            if definition['source_kind'] == 'project_config' and key not in getattr(self, 'project_ids', set()):
                continue
            history = [r for r in records if r['definition_id'] == key]
            latest = history[-1] if history else None
            failure = next((r for r in reversed(history) if r['result'] == 'failed' and not r['superseded_by']), None)
            record = failure or latest
            snapshot = snapshots[key]
            changed = python_changed(self.state['baselines'].get(key), snapshot)
            if ledger.degraded or self.config_error or config_changed or snapshot['quality'] != 'complete':
                status = 'unknown'
            elif record is None:
                status = 'missing'
            elif record['result'] == 'failed':
                status = 'failed'
            elif record['result'] in {'blocked', 'cancelled', 'timeout', 'running'}:
                status = 'incomplete'
            elif record['result'] == 'unknown' or record['validity'] == 'unknown':
                status = 'unknown'
            elif record['validity'] == 'stale':
                status = 'stale'
            else:
                status = 'passed'
            # An unbound command cannot prove this check, but neither can we
            # describe an unrecognized checking attempt as 'no verification'.
            unbound = bool(records and not records[-1]['definition_id'])
            if status == 'missing' and unbound:
                status = 'unknown'
            observation = record['observation_id'] if record else ''
            fact = digest([self.runtime.current.task_id, key, status, snapshot['fingerprint'], observation])
            rows.append(dict(definition_id=key, command=definition['command'], cwd=definition['cwd'],
                             inputs=list(definition['inputs']), status=status, label=LABELS[status],
                             changed=changed, fact_id=fact, observation_id=observation,
                             result=record['result'] if record else None,
                             validity=record['validity'] if record else None,
                             output_id=record['output_id'] if record else None))
        # Unbound/unsupported results are visible, never promoted to scoped success.
        if records and not records[-1]['definition_id']:
            r = records[-1]
            rows.append(dict(definition_id='', command=r['command'], cwd=r['cwd'], inputs=[],
                             status='unknown', label=LABELS['unknown'], changed=False,
                             observation_id=r['observation_id'], result=r['result'], validity='unknown',
                             output_id=r['output_id']))
        payload = dict(task_id=self.runtime.current.task_id, checks=rows,
                       message='仅表示列出的检查和输入范围，不代表整个任务已完成。' if rows else '尚无可用的检查配置，范围或结果未知。',
                       configuration_valid=not self.config_error and not config_changed,
                       mode=self.runtime.config.verification)
        signature = digest(payload)
        if signature != self.last_status:
            self.emit('cvm.verification_status', payload)
            self.last_status = signature
        self.observe_rows(rows)
        return rows

    def collect(self, reminders):
        if not self.enabled:
            return
        rows = self.refresh()
        self.pending = None
        contract = self.runtime.current
        eligible = (self.can_advise() and not self.state['suppressed']
                    and contract.status == 'active' and contract.confidence == 'confirmed'
                    and not any(r.status == 'pending' for r in contract.revisions))
        for row in rows:
            if (not eligible or row['status'] not in {'missing', 'failed', 'stale'}
                    or not (row['changed'] or row['status'] == 'failed')):
                continue
            key = row['definition_id']
            first = self.state['waiting'].setdefault(key, self.state['batches'])
            if self.state['batches'] <= first or row['fact_id'] in self.state['delivered']:
                continue  # Give normal edit -> test a full tool-batch opportunity.
            if len(self.state['delivered']) >= 512:
                break
            text = (f"[CVM 验证建议 {row['fact_id']}]\n{row['label']}。"
                    f"检查：{row['command']}；工作目录：{row['cwd']}。"
                    '在当前用户允许检查的前提下，可运行这项检查；否则如实说明未验证。'
                    '只针对配置范围，不代表全部功能正确。')
            if len(text) > 900:
                continue
            self.pending = dict(row=row, text=text)
            if row['fact_id'] not in self.candidate_ids:
                self.emit('cvm.verification_advice', dict(phase='candidate', fact_id=row['fact_id'],
                          definition_id=key, reason=row['status'], mode=self.runtime.config.verification))
                self.candidate_ids.add(row['fact_id'])
            break
        active = self.runtime.config.verification == 'active'
        if active:
            reminders['cvm_verification'] = self.pending['text'] if self.pending else ''
        self.save()

    def request_completed(self, messages):
        if not self.enabled or not self.pending or self.runtime.config.verification != 'active':
            return
        # Completion confirms a client request carrying the full advice got a
        # response. A failed/cancelled call or a trimmed candidate is not delivered.
        if not any(m.get('role') == 'user' and self.pending['text'] in extract_text(m.get('content')) for m in messages):
            return
        row = self.pending['row']
        self.state['delivered'][row['fact_id']] = dict(definition_id=row['definition_id'],
                                                     after=len(self.runtime.evidence.verifications), seen=[])
        self.save()
        self.emit('cvm.verification_advice', dict(phase='delivered', fact_id=row['fact_id'],
                  definition_id=row['definition_id'], reason=row['status'], boundary='model_client_response'))
        self.pending = None

    def tools_finished(self):
        if not self.enabled:
            return
        self.state['batches'] += 1
        self.save()

    def observe_rows(self, rows):
        # Called after boundary refresh, so even a parallel write after a check
        # cannot turn a follow-up's old pass into a claim about current files.
        affected = {r['definition_id'] for r in rows if r['status'] == 'failed'
                    or (r['changed'] and r['status'] in {'missing', 'stale'})}
        for key in list(self.state['waiting']):
            if key not in affected:
                self.state['waiting'].pop(key)
        for key in affected:
            self.state['waiting'].setdefault(key, self.state['batches'])
        ledger = self.runtime.evidence
        observations = {o['id']: o for o in ledger.observations}
        for fact, delivery in self.state['delivered'].items():
            for record in ledger.verifications[delivery['after']:]:
                if record['definition_id'] != delivery['definition_id'] or record['observation_id'] in delivery['seen']:
                    continue
                delivery['seen'].append(record['observation_id'])
                self.emit('cvm.verification_advice', dict(phase='followup', fact_id=fact,
                          definition_id=record['definition_id'], observation_id=record['observation_id'],
                          attempted=observations[record['observation_id']]['executed'] is True,
                          completed=record['result'] in {'passed', 'failed'},
                          passed=record['result'] == 'passed', current=record['validity'] == 'fresh'))
        self.save()
