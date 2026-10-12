"""Runtime-owned observations and version-bound checks, with no execution policy."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform

from codeagent.cvm.snapshot import capture, digest, same_version
from codeagent.cvm.verification import VerificationDefinition, simple_check, test_result
from codeagent.tools.workspace import WorkspaceGuard


class EvidenceLedger:
    def __init__(self, workspace):
        self.workspace = os.path.normcase(str(Path(workspace).resolve()))
        self.definitions = {}
        self.observations = []
        self.verifications = []
        self.diagnostics = []
        self.degraded = False
        self._seen = set()

    def fail(self, reason):
        self.degraded = True
        if reason not in self.diagnostics:
            self.diagnostics.append(reason)
        for record in self.verifications:
            record['validity'] = 'unknown'

    def register(self, definition):
        if not isinstance(definition, VerificationDefinition):
            raise TypeError('Expected trusted VerificationDefinition')
        # Resolve under the same confinement policy as ordinary file tools.
        guard = WorkspaceGuard(Path(self.workspace))
        cwd = guard.resolve(definition.cwd)
        for item in definition.inputs:
            guard.resolve(item, base=cwd)
        self.definitions[definition.id] = asdict(definition)
        return definition.id

    def receive_definitions(self, text, *, source_ref, source_kind, task_id):
        # A literal JSON declaration from a real user/approved plan. Never parse tool text.
        for line in text.splitlines():
            if not line.startswith('验证配置：'):
                continue
            try:
                value = json.loads(line.split('：', 1)[1])
                if set(value) - {'command', 'inputs', 'cwd'}:
                    raise ValueError('unknown_definition_fields')
                self.register(VerificationDefinition(**value, source_ref=source_ref,
                              source_kind=source_kind, task_id=task_id))
            except (ValueError, TypeError, KeyError, OSError, RecursionError):
                if 'invalid_user_verification_definition' not in self.diagnostics:
                    self.diagnostics.append('invalid_user_verification_definition')

    def prepare(self, tool, *, cwd, trusted_process, task_id, task_revision=None):
        command = tool.input.get('command', '') if tool.name == 'bash' else ''
        check = simple_check(command) if command else None
        definition_id, definition = '', None
        if check:
            for key, item in self.definitions.items():
                expected = os.path.normcase(str((Path(self.workspace) / item['cwd']).resolve()))
                if item['command'] == command and expected == os.path.normcase(str(Path(cwd).resolve())) and item['task_id'] == task_id:
                    definition_id, definition = key, deepcopy(item)
        before = capture(self.workspace, definition['inputs'], cwd=cwd) if definition else None
        return {'cwd': str(cwd), 'command': command, 'check': check, 'definition_id': definition_id,
                'definition': definition, 'before': before, 'trusted_process': trusted_process,
                'task_id': task_id, 'task_revision': task_revision}

    def packet(self, tool, output, prepared, *, run_id, agent_id, attempt,
               executed=True, cancelled=False, missing_status='unknown'):
        """Build detached facts before hooks/projection; workers never mutate the ledger."""
        execution_id = getattr(output, 'execution_id', '') if output is not None else ''
        identity = [run_id, agent_id, tool.id, attempt, execution_id]
        get = lambda name, default=None: getattr(output, name, default)
        observation = {
            'id': digest(identity), 'run_id': run_id, 'agent_id': agent_id,
            'task_id': prepared['task_id'], 'tool_use_id': tool.id, 'attempt': attempt,
            'task_revision': prepared['task_revision'],
            'execution_id': execution_id, 'tool': tool.name,
            'executed': (None if executed else False) if output is None else
                        bool(executed and get('status') != 'blocked' and get('outcome') != 'parameter_error'),
            'status': 'cancelled' if cancelled else get('status', missing_status),
            'exit_code': get('exit_code'), 'outcome': get('outcome', ''),
            'input_fingerprint': digest(tool.input),
            'output_fingerprint': hashlib.sha256(str(output).encode()).hexdigest() if output is not None else '',
            'output_fingerprint_scope': 'returned_tool_output',
            'cwd': prepared['cwd'], 'changed_files': list(get('changed_files', ())),
            'input_state': get('input_state', ''), 'state_known': get('state_known', False),
            'process_id': get('process_id'), 'process_running': get('process_running', False),
            'source_complete': get('source_complete', False), 'scan_complete': get('scan_complete'),
            'has_more': get('has_more', False), 'returned_range': get('returned_range'),
            'truncated_reason': get('truncated_reason'), 'storage_error': get('storage_error'),
            'output_id': get('output_id'), 'duration_seconds': get('duration_seconds', 0),
            # No inference about absence or business correctness from free-form output.
            'absence_proven': False,
        }
        record = None
        if prepared['command']:
            check = prepared['check']
            result, summary = ('cancelled' if cancelled else 'unknown', {})
            if check and prepared['trusted_process'] and output is not None:
                result, summary = test_result(output, check['runner'], executed=executed, cancelled=cancelled)
            definition = prepared['definition']
            after = capture(self.workspace, definition['inputs'], cwd=prepared['cwd']) if definition else None
            before = prepared['before']
            validity = ('fresh' if same_version(before, after) else 'stale'
                        if before['quality'] == after['quality'] == 'complete' else 'unknown') if definition else 'unknown'
            record = {'observation_id': observation['id'], 'definition_id': prepared['definition_id'],
                      'task_id': prepared['task_id'], 'command': prepared['command'], 'cwd': prepared['cwd'],
                      'task_revision': prepared['task_revision'], 'coverage': 'declared_inputs_only',
                      'runner': check['runner'] if check else 'unsupported',
                      'check_scope': check['scope'] if check else [], 'before': before, 'after': after,
                      'result': result, 'summary': summary, 'validity': validity,
                      'superseded_by': '', 'output_id': get('output_id'),
                      'environment': {'host_python': platform.python_version(), 'platform': platform.system(),
                                      'runner_version': None, 'external_dependencies': 'not_frozen'}}
        return {'observation': observation, 'verification': record}

    def commit(self, packet):
        observation, record = packet['observation'], packet['verification']
        if observation['id'] in self._seen:
            return
        if len(self.observations) >= 5000:
            self.fail('observation_limit')
            return
        self._seen.add(observation['id'])
        self.observations.append(deepcopy(observation))
        if record is not None:
            record = deepcopy(record)
            if self.degraded:
                record['validity'] = 'unknown'
            if record['result'] == 'passed' and record['validity'] == 'fresh' and record['definition_id']:
                for old in self.verifications:
                    if old['definition_id'] == record['definition_id'] and old['result'] == 'failed' and not old['superseded_by']:
                        old['superseded_by'] = record['observation_id']
            self.verifications.append(record)

    def refresh(self):
        cache = {}
        for record in self.verifications:
            if record['validity'] != 'fresh':
                continue
            definition = self.definitions.get(record['definition_id'])
            if definition is None or self.degraded:
                record['validity'] = 'unknown'
                continue
            key = (record['cwd'], tuple(definition['inputs']))
            if key not in cache:
                cache[key] = capture(self.workspace, definition['inputs'], cwd=record['cwd'])
            current = cache[key]
            record['latest_snapshot'] = current
            if current['quality'] != 'complete':
                record['validity'] = 'unknown'
            elif not same_version(record['after'], current):
                record['validity'] = 'stale'

    def export(self):
        body = deepcopy({'schema_version': 1, 'policy_version': 1, 'definitions': self.definitions,
                         'observations': self.observations, 'verifications': self.verifications,
                         'diagnostics': self.diagnostics, 'degraded': self.degraded})
        return {**body, 'content_sha256': digest(body)}

    def restore(self, payload):
        if (type(payload) is not dict or type(payload.get('schema_version')) is not int
                or type(payload.get('policy_version')) is not int
                or payload.get('schema_version') != 1 or payload.get('policy_version') != 1):
            raise ValueError('unsupported_evidence_schema')
        if payload.get('content_sha256') != digest({k: v for k, v in payload.items() if k != 'content_sha256'}):
            raise ValueError('invalid_evidence_fingerprint')
        for key, value in payload['definitions'].items():
            definition = VerificationDefinition(**value)
            if key != definition.id:
                raise ValueError('invalid_definition_fingerprint')
            self.register(definition)
        observations, records = payload['observations'], payload['verifications']
        if not isinstance(observations, list) or not isinstance(records, list) or len(observations) > 5000:
            raise ValueError('invalid_observations')
        for observation in observations:
            identity = [observation[k] for k in ('run_id', 'agent_id', 'tool_use_id', 'attempt', 'execution_id')]
            if observation['id'] != digest(identity) or observation['id'] in self._seen:
                raise ValueError('invalid_observation_identity')
            self._seen.add(observation['id'])
        for record in records:
            if (record['observation_id'] not in self._seen
                    or record['result'] not in {'passed', 'failed', 'blocked', 'cancelled', 'timeout', 'running', 'unknown'}
                    or record['validity'] not in {'fresh', 'stale', 'unknown'}
                    or (record['superseded_by'] and record['superseded_by'] not in self._seen)):
                raise ValueError('invalid_verification')
            if record['definition_id']:
                if record['definition_id'] not in self.definitions:
                    raise ValueError('missing_definition')
                for snapshot in (record['before'], record['after']):
                    body = {k: snapshot[k] for k in ('cwd', 'inputs', 'files', 'directories', 'excluded_directories')}
                    if digest(body) != snapshot['fingerprint']:
                        raise ValueError('invalid_snapshot_fingerprint')
                if record['validity'] == 'fresh' and not same_version(record['before'], record['after']):
                    raise ValueError('invalid_fresh_verification')
            elif record['validity'] != 'unknown':
                raise ValueError('unbound_verification')
        self.observations, self.verifications = deepcopy(observations), deepcopy(records)
        if (type(payload['degraded']) is not bool or not isinstance(payload['diagnostics'], list)
                or any(not isinstance(item, str) for item in payload['diagnostics'])):
            raise ValueError('invalid_evidence_diagnostics')
        self.diagnostics, self.degraded = list(payload['diagnostics']), payload['degraded']
        self.refresh()
