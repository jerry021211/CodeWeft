"""Opt-in paid smoke test of all three native collaboration paths.

Run: python -m evals.native_round2_live
Uses configured credentials, an isolated fixture/data directory, and bounded budgets.
Never modifies the configured user's project or prints credentials/model payloads.
"""
from dataclasses import replace
import argparse
from datetime import datetime, UTC
import json
import os
from pathlib import Path
import sys
import subprocess
import threading
from uuid import uuid4

from codeagent.config import EnvironmentConfig
from codeagent.events import RecordingEventSink, EventEmitter, ExecutionContext
from codeagent.permissions import WaitingPermissionBroker
from codeagent.multiagent.executions import ExecutionRepository
from codeagent.runtime import CancellationToken, RuntimeDataPaths
from codeagent.web.factory import WebAgentFactory
from codeagent.web.storage import SQLiteRepository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['all', 'single', 'batch', 'plan'], default='all')
    parser.add_argument('--archive', action='store_true', help='Verify member usage and archive this isolated fixture after completion')
    args = parser.parse_args()
    archive_fixture = args.archive
    selected_mode = args.mode
    env = EnvironmentConfig.from_env()
    os.environ['LANGSMITH_TRACING'] = 'false'
    os.environ['LANGCHAIN_TRACING_V2'] = 'false'
    root = Path('eval-results').resolve() / ('native-round2-' + datetime.now(UTC).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex[:8])
    root.mkdir(parents=True)
    report = dict(model=env.model_id, cases=[])
    print(f'Results: {root}\nModel: {env.model_id}', flush=True)
    env = replace(env, data_dir=root / 'runtime', max_tokens=4096, enable_skills=False,
        mcp_config_path=root / 'no-mcp.json', team_runtime_enabled=False,
        memory_config=replace(env.memory_config, enabled=False),
        embedding_config=replace(env.embedding_config, enabled=False),
        lsp_config=replace(env.lsp_config, enabled=False),
        loop_guard_config=replace(env.loop_guard_config, max_active_seconds=240, max_total_tokens=120_000),
        recovery_config=replace(env.recovery_config, max_retries=1, max_continuations=0, escalated_max_tokens=4096))
    with SQLiteRepository(root / 'state.db', recover_incomplete=False) as repo:
        for mode, tool in [('single', 'delegate_task'), ('batch', 'delegate_batch'), ('plan', 'execute_plan')]:
            if selected_mode not in {'all', mode}:
                continue
            workspace = root / mode
            workspace.mkdir()
            subprocess.run(['git', 'init', '--quiet', str(workspace)], check=True, capture_output=True)
            (workspace / 'fixture.txt').write_text('The fixture value is 42. No dependencies are required.\n', encoding='utf-8')
            conv = repo.create_conversation(workspace=workspace)
            run = repo.create_run(conv.id, status='running')
            tl = repo.ensure_conversation_task_list(conv.id)
            keys = []
            if mode == 'plan':
                specs = [dict(subject='Calculation', description='Create calculation.py with total(values) returning sum(values). Use Python standard library only.',
                    metadata={'execution': dict(profile='implement', files=['calculation.py'], checks=[{'argv': [sys.executable, '-c', 'from calculation import total; assert total([20,22]) == 42']}])}),
                    dict(subject='Acceptance', description='Read calculation.py first. Create test_calculation.py using unittest with positive, negative and empty input tests. Do not modify calculation.py.',
                    metadata={'execution': dict(profile='verify', files=['test_calculation.py'], checks=[{'argv': [sys.executable, '-m', 'unittest', 'test_calculation']}])})]
            else:
                specs = [dict(subject='Inspect fixture ' + str(i), description='Read fixture.txt once and report its value. Do not modify files or run commands.',
                    metadata={'execution': dict(profile='research')}) for i in range(2 if mode == 'batch' else 1)]
            for index, spec in enumerate(specs):
                keys.append(repo.create_task(tl.id, **spec, blocked_by=keys[:1] if mode == 'plan' and index else []).task.id)
            args = {'taskId': keys[0]} if mode == 'single' else {'taskIds': keys}
            marker = 'GLOBAL-R2-' + mode.upper()
            prompt = (f'Use {tool} exactly with {json.dumps(args)} to perform the existing tasks. '
                f'Global requirement for EVERY worker: include {marker} in the summary of its final report. '
                'Use Python standard library only; never install dependencies, access the network, push or commit. '
                'Do not create/update tasks or do the work yourself. After the tool returns, give a brief truthful status and stop. '
                'If blocked, report the blocker without retries or additional work.')
            repo.create_message(conv.id, role='user', content=prompt, run_id=run.id)
            token = CancellationToken()
            timer = threading.Timer(260, lambda: token.cancel('live smoke deadline'))
            emitter = EventEmitter(RecordingEventSink(repo), context=ExecutionContext(conversation_id=conv.id, run_id=run.id))
            factory = WebAgentFactory(env, workspace, repo, data_paths=RuntimeDataPaths(env.data_dir))
            timer.start()
            print(f'{mode}: started', flush=True)
            row = dict(mode=mode, tool=tool)
            try:
                agent = factory.create(event_emitter=emitter, cancellation=token,
                                       permission_broker=WaitingPermissionBroker(default_timeout=1), native_collaboration=True)
                result = agent.run(prompt)
                executions = ExecutionRepository(repo).list(conv.id, workspace)
                row.update(stop_reason=result.stop_reason, model_calls=agent._loop_guard.budget.state.model_calls,
                           total_tokens=agent._loop_guard.budget.state.total_tokens)
                if len(executions) != 1:
                    raise AssertionError(f'Expected one execution, got {len(executions)}')
                execution = executions[0]
                row.update(status=execution['status'], issue=execution.get('issue'),
                    members=[dict(id=key, status=value['status'], attempts=value['attempts'], report=value.get('report'))
                             for key, value in execution['records'].items()], checks=execution.get('checks', []))
                assert execution['status'] == 'completed', execution.get('issue')
                assert all(marker in v.get('report', {}).get('claims', {}).get('summary', '') for v in execution['records'].values()), 'Global constraint missing from member summary'
                assert all(v.get('report', {}).get('tool_evidence') for v in execution['records'].values()), 'No observed tool evidence'
                if mode == 'plan':
                    assert (workspace / 'calculation.py').exists() and (workspace / 'test_calculation.py').exists()
                    assert not list(workspace.rglob('*.pyc')), 'Bytecode leaked into delivery'
                    assert execution.get('checks') and all(c['status'] == 'passed' for c in execution['checks'])
                if archive_fixture:
                    from codeagent.multiagent.maintenance import ExecutionResources
                    usage = ExecutionRepository(repo).usage(execution)
                    assert usage['model_calls'] > 0, 'Member calls missing from persisted usage'
                    assert usage['model_calls'] < row['model_calls'], 'Root calls mixed into member usage'
                    row['member_usage'] = usage
                    resources = ExecutionResources(ExecutionRepository(repo),
                        RuntimeDataPaths(env.data_dir).workspace_dir(workspace) / 'collaborations', execution['id'])
                    row['archive'] = resources.archive(retention_days=0)
                    assert row['archive']['files'] == 0
                    if mode == 'plan':
                        subprocess.run([sys.executable, '-B', '-m', 'unittest', 'test_calculation'], cwd=workspace, check=True, capture_output=True)
                row['passed'] = True
            except Exception as exc:
                row.update(passed=False, error=f'{type(exc).__name__}: {exc}')
            finally:
                timer.cancel()
                factory.close()
                report['cases'].append(row)
                (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f"{mode}: {'PASS' if row['passed'] else 'FAIL'} ({row.get('model_calls', '?')} model calls)", flush=True)
    return 0 if all(row['passed'] for row in report['cases']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
