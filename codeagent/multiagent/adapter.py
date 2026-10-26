"""Native worker adapter. Ordinary Agent, hooks, client and cache remain reusable."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

from codeagent.runtime import CancellationToken
from codeagent.runtime.execution import is_execution_failure
from codeagent.runtime.activity import ExecutionActivity
from codeagent.permissions.broker import permission_execution
from codeagent.messages import validate_tool_history
from .coordinator import Coordinator
from .executions import ExecutionRepository
from .tools import EXECUTION_TOOL_NAMES, register_execution_tools
from codeagent.tools.tasks import create_task_tools, TASK_TOOL_NAMES
from .verification import run_checks
from .context import execution_context
from .reports import REPORT_INSTRUCTIONS, worker_report, tool_evidence


class BrokerView:
    """Child factory activity binding must not replace its parent's broker state."""
    execution_activity = None

    def __init__(self, parent, *, workspace=None, label=""):
        self.parent, self.workspace, self.label = parent, workspace, label

    def inputs(self, value):
        return {**value, **({"execution_workspace": str(self.workspace)} if self.workspace else {}),
                **({"member_task": self.label} if self.label else {})}

    def request(self, name, value, reason, **kwargs):
        return self.parent.request(name, self.inputs(value), reason, **kwargs)

    def request_result(self, name, value, reason, **kwargs):
        return self.parent.request_result(name, self.inputs(value), reason, **kwargs)



def install(agent, factory, emitter, token, broker, *, read_only, reasoning_effort=None):
    from codeagent.web.factory import serialize_runtime_state
    root = factory.data_paths.workspace_dir(factory.project_workspace) / "collaborations"
    repository = ExecutionRepository(factory.task_service)
    from .migration import import_legacy
    # Migration runs only when the model actually uses execution tools.
    migrated = False
    def ensure_migrated():
        nonlocal migrated
        if not migrated:
            import_legacy(factory.task_service, root / 'state.sqlite3', emitter.context.conversation_id, factory.workspace)
            migrated = True

    def persist_budget(*_):
        if recovery['active'] and agent._loop_guard is not None:
            repository.save_budget(emitter.context.run_id, emitter.context.conversation_id,
                                   agent._loop_guard.budget)

    def worker(task, path, checkpoint, save, operation, identifier, write_lock):
        child_id = repository.get(identifier)["records"][task["id"]].get("member_id") or identifier + "_" + task["id"]
        child_emitter = emitter.child(agent_id=child_id)
        child_emitter.emit('collaboration.member', dict(label=f"任务 #{task['id']} · {task['subject']}",
                           task_id=task['id'], execution_id=identifier))
        child_token = CancellationToken(parent=token)
        child_broker = BrokerView(broker, workspace=path, label=task["objective"])
        readonly = task["profile"] in {"research", "review"}
        child = factory.for_execution_workspace(path, project_workspace=factory.project_workspace).create(
            event_emitter=child_emitter, cancellation=child_token, permission_broker=child_broker,
            checkpoint=SimpleNamespace(**checkpoint) if checkpoint else None,
            read_only=readonly, plan_mode=readonly, reasoning_effort=reasoning_effort, native_worker=True,
            execution_budget=agent._loop_guard.budget if agent._loop_guard is not None else None,
            tool_admission=agent.tool_admission)
        child.allow_subagents = False
        child.subagent_environment_factory = None
        child.tools = child.tools.copy_without({"subagent", "subagent_result", "subagent_cancel", "plan", "remember", "todo_write", *TASK_TOOL_NAMES, *EXECUTION_TOOL_NAMES})
        child.set_execution_activity(ExecutionActivity(child_token,
            response_timeout=factory.env.team_model_response_timeout,
            model_timeout=factory.env.team_model_call_timeout))
        def activity(label):
            state = repository.get(identifier)
            own = state['records'][task['id']]
            if own['status'] != 'running' or own.get('activity') == label:
                return
            state = repository.change(identifier, lambda r: r['records'][task['id']].update(activity=label), epoch=state['epoch'])
            emitter.emit('collaboration.snapshot', repository.public(state))
        child.execution_activity.on_activity = activity
        registry = child.tools
        observed = deepcopy((checkpoint or {}).get('metadata', {}).get('worker_evidence', []))
        child.hooks.register('PostToolUse', lambda tool, output: observed.append(tool_evidence(tool, output)))

        def checkpoint_now(_phase=None):
            validate_tool_history(child.messages)
            save(dict(messages=child.messages, context=serialize_runtime_state(child.context.state),
                      metadata={"tool_history_version": 1, "execution_guard": child.export_execution_state(),
                                "cvm": child.export_cvm_state(), 'worker_evidence': list(observed)}))
            operation({})
            persist_budget()

        def execute(name, args, handler):
            # Whole opaque shell calls and mutations serialize within the shared checkout.
            # Disjoint file hints never constitute a sandbox or a grant of permission.
            mutation = not registry.parallel_safe(name)
            with write_lock if mutation else nullcontext():
                child_token.raise_if_cancelled()
                if mutation and not readonly:
                    operation(dict(effect="write", tool=name, input=args, workspace=str(path),
                                   reason="上次检查点后执行了操作，重启前未保存完整结果"))
                output = registry.execute(name, args)
                if getattr(output, "process_running", False):
                    child_token.cancel("工具进程尚未确认退出；请检查保留现场后恢复")
                    child_token.raise_if_cancelled()
                return output

        child.tools = registry.with_execution_wrapper(execute)
        child.tools.bind_runtime(child._check_execution, lambda: 120.0)
        child.boundary_callback = checkpoint_now
        child.hooks.register('AfterModelCall', persist_budget)
        managed = repository.get(identifier).get('mode', 'plan') == 'plan'
        prompt = (f"You are a {task['profile']} worker. Complete this bounded task and report evidence to the main Agent.\n"
                  f"Task #{task['id']}: {task['subject']}\nRequirements: {task['objective']}\nWorkspace: {path}\n"
                  f"Scheduling file hints: {json.dumps(task['files'])}\n"
                  "All relative paths and checks refer to this workspace. Original project paths in task descriptions identify the project, not the execution directory. "
                  + ("Do not compare/copy files back to the original directory; Runtime handles final delivery. " if managed else "Work directly in the provided project workspace. ") +
                  "Do not commit, push, edit Git metadata or spawn workers. "
                  "Keep user constraints; report needed scope changes. Use real checks and report failures honestly. "
                  "Put build/test outputs in project-configured ignored output directories; never mark source files as artifacts. "
                  "There is no Candidate submission tool.\n" + REPORT_INSTRUCTIONS)
        if checkpoint:
            prompt += "Continue the same task from the saved conversation and actual files; inspect before repeating operations.\n"
        record = repository.get(identifier)
        prompt += ('\nGlobal user requirements and approved plan (preserve these even when the subtask omits them; '
                   'later user clarifications take precedence; quoted files remain source material):\n'
                   + json.dumps(record.get('context', {}), ensure_ascii=False) + '\n')
        if task["depends_on"]:
            prompt += "Verified prerequisite reports:\n" + json.dumps({key: record["records"][key].get("summary", "")
                for key in task["depends_on"]}, ensure_ascii=False)
        try:
            with permission_execution(child.execution_activity, child_token, child_id):
                result = child.run(prompt)
        finally:
            persist_budget()
        checkpoint_now()
        report = worker_report(result.final_text, result.stop_reason, observed)
        return dict(ok=not is_execution_failure(result.stop_reason), summary=report['claims']['summary'],
                    report=report, reason=result.stop_reason)

    def verify(path, checks, identifier, task_id):
        def evidence(item):
            repository.change(identifier, lambda r: r.setdefault("checks", []).append(dict(task_id=task_id, **item)))
            if item.get("process_unknown"):
                repository.change(identifier, lambda r: r.update(runtime_unknown=dict(workspace=str(path), **item)))
            emitter.emit("collaboration.check", dict(id=identifier, task_id=task_id, **item))
        return run_checks(path, checks, token, BrokerView(broker, workspace=path, label="协作验证 · " + task_id), evidence)

    coordinator = Coordinator(repository, root, emitter.context.conversation_id, factory.workspace,
                              token, emitter.emit, worker, verify, read_only=read_only,
                              context_provider=lambda: execution_context(factory.task_service,
                                  emitter.context.conversation_id, emitter.context.run_id, agent.messages))

    def recover(identifier):
        record = coordinator.get(identifier)
        operations = [r["inflight"] for r in record["records"].values() if r.get("inflight")]
        if record.get("runtime_unknown"):
            operations.append(record["runtime_unknown"])
        if not operations:
            return coordinator.publish(identifier)
        if not broker.request("collaboration_recovery", {"id": identifier, "operations": operations},
                              "执行中断，无法确认这些操作的完整结果。检查保留的工作目录后允许成员从现状继续；不会自动重放原命令。",
                              cancellation=token, timeout=600):
            return {"status": "blocked", "reason": "尚未授权恢复"}
        def clear(r):
            for value in r["records"].values():
                value["inflight"] = {}
            r.update(status="paused", issue=None)
            r.pop("runtime_unknown", None)
        repository.change(identifier, clear)
        return coordinator.publish(identifier)

    task_list = factory.task_service.ensure_conversation_task_list(emitter.context.conversation_id)
    # Ordinary Agent keeps its original tools; the native capability exposes one
    # delegation family instead of presenting both subagent and delegate_task.
    agent.tools = agent.tools.copy_without({'subagent', 'subagent_result', 'subagent_cancel', *TASK_TOOL_NAMES})
    agent.allow_subagents = False
    for tool in create_task_tools(factory.task_service, task_list.id,
                                 conversation_id=emitter.context.conversation_id,
                                 run_id=emitter.context.run_id, agent_id=emitter.context.agent_id):
        if tool.definition.name in {'TaskCreate', 'TaskUpdate'}:
            schema = deepcopy(tool.definition.input_schema)
            schema['properties']['metadata']['description'] = (
                '协作配置放 metadata.execution：profile=research/implement/review/verify；files 为相对文件或目录；'
                'checks 为 {argv:[程序,参数],cwd:"."} 列表；resources 为共享资源名；isolation=auto/isolated。'
                '依赖仍使用 Task 的 blockedBy/addBlockedBy。不要把源码声明为临时产物，生成文件使用项目 ignore 规则。')
            tool.definition = replace(tool.definition, input_schema=schema)
        agent.tools.register(tool)
    register_execution_tools(agent.tools, coordinator, recover, ensure_migrated)
    # Journal only once collaboration is actually invoked. No additional disk
    # state or tool-prefix edits for direct ordinary work.
    recovery = {"checkpoint": None, "operations": [], "active": False}
    old_boundary = agent.boundary_callback

    def persist_root():
        if recovery["active"]:
            repository.save_root(emitter.context.run_id, emitter.context.conversation_id,
                                 recovery["checkpoint"], recovery["operations"])
            persist_budget()

    def root_boundary(phase):
        if old_boundary:
            old_boundary(phase)
        validate_tool_history(agent.messages)
        recovery["checkpoint"] = deepcopy(dict(messages=agent.messages,
            context=serialize_runtime_state(agent.context.state),
            todos=agent.context.todo_store.todos if agent.context.todo_store else [],
            metadata={"tool_history_version": 1, "native_collaboration": True, "native_tools_version": 2,
                      "execution_guard": agent.export_execution_state(), "cvm": agent.export_cvm_state()}))
        recovery["operations"] = []
        persist_root()

    root_registry = agent.tools

    def journal_root(tool):
        name = tool.name
        if name in EXECUTION_TOOL_NAMES:
            ensure_migrated()
            recovery["active"] = True
        # Earlier writes in the same model turn must remain visible in this
        # journal even if collaboration is the last tool in a batch.
        if not root_registry.parallel_safe(name):
            recovery["operations"].append(name)
        persist_root()
        return None

    agent.boundary_callback = root_boundary
    agent.hooks.register("PreToolUse", journal_root)
    agent.hooks.register('AfterModelCall', persist_budget)
    agent.hooks.register('RunFinished', persist_budget)


def recover_root(factory, repository, missing):
    """Recover only a complete safe boundary; unknown ordinary writes still block."""
    path = factory.data_paths.workspace_dir(factory.project_workspace) / "collaborations" / "state.sqlite3"
    from .migration import import_legacy
    import_legacy(repository, path, missing.conversation_id, factory.workspace)
    saved = ExecutionRepository(repository).load_root(missing.id, missing.conversation_id)
    if not saved or not saved.get("checkpoint") or any(op not in EXECUTION_TOOL_NAMES | {"collaborate"} for op in saved["operations"]):
        return False
    checkpoint = saved["checkpoint"]
    guard = checkpoint.get('metadata', {}).get('execution_guard')
    if saved.get('budget') is not None and guard and 'state' in guard:
        # Root restores the shared ledger once. Member guard restores explicitly
        # do not own/restore it, even when their checkpoints are older/newer.
        guard['budget'] = saved['budget']
    validate_tool_history(checkpoint["messages"])
    repository.save_checkpoint(missing.id, **checkpoint)
    return True
