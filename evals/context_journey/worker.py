"""One process, one workspace, one Agent and conversation across all user turns."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import time

from codeagent.events import EventEmitter, ExecutionContext
from codeagent.events.sink import RecordingEventSink
from codeagent.permissions import WaitingPermissionBroker
from codeagent.runtime import CancellationToken
from codeagent.tools import tool_schema_hash
from codeagent.web.factory import serialize_runtime_state
from codeagent.web.storage import SQLiteRepository
from evals.agent_adapter import append_record, build_agent
from evals.evidence import read_json, write_json
from evals.tracing import run_with_trace
from evals.context_journey.offline import OfflineSDK
from evals.context_journey.runtime import JourneyObserver, register_tools


def execute(trial):
    trial = trial.resolve()
    spec, seed = read_json(trial / "manifest.json"), read_json(trial / "seed.json")
    workspace = trial / "workspace"
    observer = JourneyObserver(trial, workspace, seed)
    agent = factory = recorder = None
    result = {"execution_status": "setup_error", "completed_phases": 0}
    started = time.monotonic()
    anchors = []
    import codeagent
    write_json(trial / "worker-runtime.json", {"engine_package": codeagent.__file__, "worker": __file__})
    with SQLiteRepository(trial / "runtime/state.db", recover_incomplete=False) as repository:
        conversation = repository.create_conversation(title=seed["title"], workspace=workspace)
        recording = RecordingEventSink(repository)

        class Sink:
            durable = True

            def emit(self, event):
                recording.emit(event)
                append_record(trial / "events.jsonl", {**event.to_dict(), "phase": observer.phase})

        emitter = EventEmitter(Sink(), context=ExecutionContext(conversation_id=conversation.id))
        try:
            offline = OfflineSDK(seed, read_json(trial / "gold.json")) if spec["profile"]["mode"] == "offline" else None
            agent, factory, recorder = build_agent(spec["profile"], workspace, trial, repository, emitter,
                CancellationToken(), WaitingPermissionBroker(default_timeout=0), scripted_sdk=offline)
            register_tools(agent, observer)
            agent.context.state.tool_schema_hash = tool_schema_hash(agent.tools.schemas())
            write_json(trial / "effective-context.json", {k: str(v) if isinstance(v, Path) else v for k, v in asdict(agent.context.config).items()})
            write_json(trial / "tool-schemas.json", agent.tools.schemas())
            write_json(trial / "initial-history.json", agent.messages)
            for phase in seed["phases"]:
                observer.phase = phase["id"]
                folder = trial / "phases" / phase["id"]
                folder.mkdir(parents=True)
                run = repository.create_run(conversation.id)
                repository.update_run_status(run.id, "running")
                emitter.context = ExecutionContext(conversation_id=conversation.id, run_id=run.id, turn_id=phase["id"])
                initial_count = len(agent.messages)
                if offline:
                    offline.archive_path = next(iter(agent.context.state.tool_artifacts), None)
                    offline.begin(phase)
                write_json(folder / "before-state.json", serialize_runtime_state(agent.context.state))
                before_calls, start = recorder.count, time.monotonic()
                outcome = run_with_trace(agent, phase["prompt"], trial=folder, spec={**spec, "case_id": spec["case_id"] + "." + phase["id"]})
                if phase.get("anchor"):
                    anchors.extend(i for i in range(initial_count, len(agent.messages))
                                   if agent.messages[i].get("role") == "user" and agent.messages[i].get("content") == phase["prompt"])
                    # Also age out this stage's response and tool evidence: T03
                    # must not pass coverage merely because its initial request
                    # was folded while the successful test remained in the tail.
                    anchors.append(len(agent.messages) - 1)
                state = serialize_runtime_state(agent.context.state)
                observer.scan("phase_end")
                phase_result = {"id": phase["id"], "stop_reason": outcome.stop_reason, "iterations": outcome.iterations,
                                "final_text": outcome.final_text, "api_calls": recorder.count - before_calls,
                                "duration_ms": round((time.monotonic() - start) * 1000, 3), "anchor_indices": anchors[:],
                                "state": state, "last_compaction": dict(agent.context.last_compaction),
                                "workspace_hashes": observer.previous}
                write_json(folder / "result.json", phase_result)
                write_json(folder / "messages.json", agent.messages)
                append_record(trial / "phases.jsonl", phase_result)
                repository.finish_run_with_checkpoint(run.id,
                    status="completed" if outcome.stop_reason == "end_turn" else "failed", messages=agent.messages,
                    todos=[], context=state, checkpoint_metadata={"tool_history_version": 1})
                print(f'{phase["id"]}: {outcome.stop_reason}, calls={recorder.count - before_calls}', flush=True)
                if outcome.stop_reason != "end_turn":
                    result.update(execution_status="agent_failed", stop_reason=outcome.stop_reason)
                    break
                result["completed_phases"] += 1
            else:
                result.update(execution_status="completed", stop_reason="end_turn")
        except Exception as exc:
            result.update(execution_status="error", error_type=type(exc).__name__, error=str(exc))
        finally:
            if agent:
                write_json(trial / "final-state.json", serialize_runtime_state(agent.context.state))
                write_json(trial / "messages.json", agent.messages)
            if recorder:
                result["api_calls"] = recorder.count
                recorder.close()
            if factory:
                factory.close()
            observer.scan("worker_end")
            result.update(duration_ms=round((time.monotonic() - started) * 1000, 3), anchor_indices=anchors)
            write_json(trial / "worker-result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--trial", type=Path, required=True)
    execute(parser.parse_args().trial)
