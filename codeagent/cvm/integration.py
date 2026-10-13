"""Agent-local CVM wiring; contract and evidence policy stay in their own modules.

Observers run on the parent thread. Tool attempts only capture private packets;
the ToolBatchFinished observer commits them in request/attempt order, including
failed, cancelled and unstarted calls. This is intentionally separate from the
legacy PostToolUse control chain, which can short-circuit or mutate its output.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from dataclasses import replace

from codeagent.cvm.state import CvmRuntime
from codeagent.cvm.supervision import VerificationSupervisor
from codeagent.hooks.manager import HookManager
from codeagent.messages import ToolUse
from codeagent.tools.base import ToolOutput
from codeagent.tools.registry import ToolRegistry


class CvmHooks:
    # A child Agent installs fresh observers against its own runtime.
    agent_local = True

    def __init__(self, runtime: CvmRuntime, recorder: CvmToolRecorder, *, emit=lambda *args: None, can_advise=lambda: False):
        self.runtime = runtime
        self.recorder = recorder
        self.supervisor = VerificationSupervisor(runtime, emit=emit, can_advise=can_advise)

    def install(self, hooks: HookManager) -> None:
        hooks.observe('UserInputAccepted', self.receive_input)
        hooks.observe('RunInputReady', self.project_goal)
        hooks.observe('CollectRuntimeReminders', self.collect_reminders)
        hooks.observe('ToolBatchFinished', self.commit_tools)
        hooks.observe('RunFinished', self.finish_run)
        hooks.observe('Stop', self.before_stop)
        hooks.observe('ModelRequestCompleted', self.supervisor.request_completed)

    def receive_input(self, prompt, *, run_id, reference, update) -> None:
        self.runtime.receive(prompt, run_id=run_id, reference=reference, update=update)
        self.recorder.register_definitions()
        self.supervisor.begin(prompt)

    def project_goal(self, context_state) -> None:
        goal = self.runtime.user_goal()
        if goal is not None:
            context_state.user_goal = goal

    def collect_reminders(self, reminders: dict[str, str]) -> None:
        if self.runtime.active:
            reminders['cvm_contract'] = self.runtime.reminder()
        self.supervisor.collect(reminders)

    def commit_tools(self, tools, results, executions) -> None:
        self.recorder.commit(tools, results, executions)
        self.supervisor.tools_finished()

    def before_stop(self, messages) -> None:
        self.supervisor.refresh()

    def finish_run(self) -> None:
        self.runtime.refresh_evidence()
        self.supervisor.refresh()


class CvmToolRecorder:
    """Capture each raw attempt before any tool-result hooks or projection.

    execute() may run on a worker; it only appends to that call's private list.
    commit() is parent-only. Capture failures become explicit degraded evidence,
    preserving the existing stage-2 behavior without changing tool execution.
    """

    def __init__(self, runtime: CvmRuntime, tools: Callable[[], ToolRegistry], workspace: Callable[[], Path],
                 identity: Callable[[], tuple[str, str]]):
        self.runtime = runtime
        self.tools = tools
        self.workspace = workspace
        self.identity = identity

    def execute(self, tool: ToolUse, invoke: Callable[[], ToolOutput], *, packets, attempt: int) -> ToolOutput:
        prepared = None
        if self.runtime.recording and packets is not None:
            try:
                cwd, trusted = self.tools().verification_context(tool.name, self.workspace())
                prepared = self._prepare(tool, cwd=cwd, trusted_process=trusted)
            except Exception:
                packets.append({'error': 'evidence_prepare_failed'})
        try:
            output = invoke()
        except BaseException as error:
            if prepared is not None:
                self._capture(packets, tool, getattr(error, 'tool_output', None), prepared, attempt, cancelled=True)
            raise
        if prepared is not None:
            self._capture(packets, tool, output, prepared, attempt)
        return output

    def _prepare(self, tool, *, cwd, trusted_process):
        current = self.runtime.current
        adapter = self.tools().verification_adapter(tool.name)
        command = adapter.select(tool.input) if adapter else None
        if adapter and command not in {d.command for d in adapter.definitions}:
            command = None
        return self.runtime.evidence.prepare(
            tool, cwd=cwd, trusted_process=trusted_process,
            task_id=current.task_id if current else '', task_revision=current.revision if current else None,
            host_command=command, host_result_reader=adapter.read_result if adapter and command else None,
        )

    def register_definitions(self):
        if self.runtime.recording and self.runtime.current:
            for adapter in self.tools().verification_adapters():
                for definition in adapter.definitions:
                    self.runtime.evidence.register(replace(definition, task_id=self.runtime.current.task_id))

    def _capture(self, packets, tool, output, prepared, attempt, *, cancelled=False,
                 executed=True, missing_status='unknown') -> None:
        try:
            run_id, agent_id = self.identity()
            packets.append(self.runtime.evidence.packet(
                tool, output, prepared, run_id=run_id, agent_id=agent_id, attempt=attempt,
                executed=executed, cancelled=cancelled, missing_status=missing_status,
            ))
        except Exception:
            packets.append({'error': 'evidence_capture_failed'})

    def commit(self, tools, results, executions) -> None:
        if not self.runtime.recording:
            return
        for tool, result, execution in zip(tools, results, executions):
            try:
                packets = execution.get('observation_packets', [])
                if not packets:
                    # Denials, unstarted calls and subagent reports have no process attestation.
                    prepared = self._prepare(tool, cwd=self.workspace(), trusted_process=False)
                    output = result['content'] if isinstance(result['content'], ToolOutput) else None
                    self._capture(packets, tool, output, prepared, 0,
                                  executed=execution['status'] in {'success', 'error'},
                                  missing_status=execution['status'])
                for packet in packets:
                    if 'error' in packet:
                        self.runtime.evidence.fail(packet['error'])
                    else:
                        self.runtime.evidence.commit(packet)
            except Exception:
                self.runtime.evidence.fail('evidence_commit_failed')
