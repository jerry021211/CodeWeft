"""Small real-process lifecycle probe; temporary files, no model and no network."""
from pathlib import Path
from tempfile import TemporaryDirectory

from codeagent.cvm.evidence import EvidenceLedger
from codeagent.cvm.verification import VerificationDefinition
from codeagent.messages import ToolUse
from codeagent.tools.bash import BashTool
from codeagent.tools.workspace import WorkspaceGuard


def run_probe():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'checks').mkdir()
        source = root / 'target.py'
        source.write_text('VALUE = 100\n')
        (root / 'checks' / 'test_target.py').write_text(
            'import unittest\nfrom target import VALUE\nclass T(unittest.TestCase):\n'
            ' def test_value(self): self.assertEqual(VALUE, 2)\n')
        command = 'python -m unittest discover -s checks'
        ledger = EvidenceLedger(root)
        ledger.register(VerificationDefinition(command, ('target.py', 'checks'), task_id='probe'))
        bash = BashTool(workspace_guard=WorkspaceGuard(root), output_root=root / 'logs')

        def execute(identifier):
            call = ToolUse(identifier, 'bash', {'command': command})
            before = ledger.prepare(call, cwd=root, trusted_process=True, task_id='probe')
            output = bash.run(command)
            ledger.commit(ledger.packet(call, output, before, run_id='probe-run', agent_id='root', attempt=1))
            ledger.refresh()
            return {k: ledger.verifications[-1][k] for k in ('result', 'validity', 'observation_id')}

        failure = execute('failure')
        # Different file length also avoids Python timestamp/size bytecode cache reuse.
        source.write_text('VALUE = 2\n')
        success = execute('success')
        source.write_text('VALUE = 3000\n')
        ledger.refresh()
        edited = {k: ledger.verifications[-1][k] for k in ('result', 'validity', 'observation_id')}
        state = ledger.export()
        snapshots = [s for r in state['verifications'] for s in (r['before'], r['after'], r.get('latest_snapshot')) if s]
        return {'passed': failure['result'] == 'failed' and success['result'] == 'passed'
                         and success['validity'] == 'fresh' and edited['validity'] == 'stale'
                         and bool(state['verifications'][0]['superseded_by']),
                'timeline': [failure, success, edited], 'evidence': state,
                'snapshot_metrics': {'captures_in_report': len(snapshots),
                                     'scanned_bytes': sum(s['scanned_bytes'] for s in snapshots),
                                     'duration_ms': round(sum(s['duration_ms'] for s in snapshots), 3)},
                'model_calls': 0, 'tool_calls': 2}
