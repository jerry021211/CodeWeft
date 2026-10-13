"""Reuse the production Web Agent composition root; never simulate its loop."""
import argparse
from dataclasses import asdict
from pathlib import Path
import time

from codeagent.cvm import CvmConfig
from codeagent.events import EventEmitter, ExecutionContext
from codeagent.permissions import WaitingPermissionBroker
from codeagent.runtime import CancellationToken
from codeagent.tools import tool_schema_hash
from codeagent.web.factory import serialize_runtime_state
from codeagent.web.storage import SQLiteRepository
from evals.agent_adapter import append_record, build_agent
from evals.evidence import read_json, write_json
from evals.cvm_verification.budget import Budget, BudgetedSDK
from evals.cvm_verification.offline import OfflineSDK
from evals.cvm_verification.runtime import VerificationObserver, register_tools


def execute(trial, *, observer_type=VerificationObserver, offline_factory=None,
            configure=None, register=register_tools):
    trial = Path(trial).resolve()
    manifest, seed = read_json(trial / 'manifest.json'), read_json(trial / 'seed.json')
    profile = manifest['profile']
    observer = observer_type(trial, trial / 'workspace', seed)
    agent = factory = recorder = None
    result = {'execution_status': 'setup_error', 'completed_phases': 0}
    started = time.monotonic()
    with SQLiteRepository(trial / 'runtime/state.db', recover_incomplete=False) as repository:
        conversation = repository.create_conversation(title=seed['title'], workspace=observer.workspace)
        class Sink:
            durable = True
            def emit(self, event):
                append_record(trial / 'events.jsonl', {**event.to_dict(), 'phase': observer.phase})
        emitter = EventEmitter(Sink(), context=ExecutionContext(conversation_id=conversation.id))
        try:
            offline = ((offline_factory(seed) if offline_factory else OfflineSDK(seed['case_id']))
                       if profile['mode'] == 'offline' else None)
            agent, factory, recorder = build_agent(profile, observer.workspace, trial, repository, emitter,
                CancellationToken(), WaitingPermissionBroker(default_timeout=0), scripted_sdk=offline)
            if not offline:
                recorder.sdk = BudgetedSDK(recorder.sdk, Budget(trial.parent / 'budget.db'))
            if configure:
                configure(agent, manifest, trial)
            else:
                agent.config.cvm = CvmConfig(contract='shadow', evidence='shadow', verification=manifest['variant'])
            register(agent, observer)
            agent.context.state.tool_schema_hash = tool_schema_hash(agent.tools.schemas())
            write_json(trial / 'effective-config.json', {'cvm': agent.config.cvm.snapshot(),
                       'context': {k: str(v) if isinstance(v, Path) else v for k, v in asdict(agent.context.config).items()},
                       'tool_schemas': agent.tools.schemas(), 'model': agent.config.model})
            for phase in seed['phases']:
                observer.phase = phase['id']
                run = repository.create_run(conversation.id)
                repository.update_run_status(run.id, 'running')
                emitter.context = ExecutionContext(conversation_id=conversation.id, run_id=run.id, turn_id=phase['id'])
                if offline:
                    offline.begin(phase)
                outcome = agent.run(phase['prompt'])
                observer.scan('phase_end')
                append_record(trial / 'phases.jsonl', {'id': phase['id'], 'stop_reason': outcome.stop_reason,
                    'iterations': outcome.iterations, 'final_text': outcome.final_text, 'workspace_hashes': observer.previous})
                cvm = agent.export_cvm_state()
                write_json(trial / 'cvm-state.json', cvm)
                repository.finish_run_with_checkpoint(run.id,
                    status='completed' if outcome.stop_reason == 'end_turn' else 'failed', messages=agent.messages,
                    todos=[], context=serialize_runtime_state(agent.context.state), checkpoint_metadata={'cvm': cvm, 'tool_history_version': 1})
                result['completed_phases'] += 1
                if outcome.stop_reason != 'end_turn':
                    result.update(execution_status='agent_failed', stop_reason=outcome.stop_reason)
                    break
            else:
                result.update(execution_status='completed', stop_reason='end_turn')
        except Exception as error:
            result.update(execution_status='error', error_type=type(error).__name__)
        finally:
            if agent:
                write_json(trial / 'cvm-state.json', agent.export_cvm_state())
                # Public response evidence, never credentials or hidden reasoning.
                from evals.agent_adapter import public_trace
                write_json(trial / 'messages.json', public_trace(agent.messages))
            if recorder:
                result['api_calls'] = recorder.count
                recorder.close()
            if factory:
                factory.close()
            observer.scan('worker_end')
            result['duration_ms'] = round((time.monotonic() - started) * 1000, 3)
            write_json(trial / 'worker-result.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--trial', required=True, type=Path)
    execute(parser.parse_args().trial)
