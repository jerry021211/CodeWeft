"""Minimal command-line entry point."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from codeagent import (
    Agent,
    AgentResult,
    ContextManager,
    EnvironmentConfig,
    MemoryManager,
    MemoryStore,
    PlanningBackend,
    PromptRuntime,
    PromptMode,
    RecoveryRuntime,
    SkillLoader,
    TodoStore,
    create_default_hooks,
    create_default_registry,
    resolve_planning_backend,
)
from codeagent.mcp import McpRouter
from codeagent.memory import (
    MemoryAccessController,
    MemoryAccessPolicy,
    MemoryWriteBlocked,
)
from codeagent.permissions import CliPermissionBroker, PermissionPolicy
from codeagent.runtime import RuntimeDataPaths
from codeagent.runtime.execution import is_execution_failure
from codeagent.tools import LoadContextHistoryTool, LoadToolOutputTool
from codeagent.tools.ask_user import terminal_ask_user
from codeagent.tools.search_code import SearchCodeTool
from codeagent.tools.workspace import WorkspaceGuard
from codeagent.web.storage import SQLiteRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the codeagent Anthropic agent.")
    parser.add_argument(
        "query",
        nargs="*",
        help="Prompt to run once. If omitted, starts an interactive loop.",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming text output even if STREAMING=true.",
    )
    parser.add_argument(
        "--planning-mode",
        choices=("auto", "tasks", "todo"),
        help="Planning backend. Interactive mode defaults to tasks; one-shot defaults to todo.",
    )
    parser.add_argument(
        "--task-list",
        help="Use an existing task list when the tasks backend is active.",
    )
    parser.add_argument(
        "--read-only", action="store_true",
        help="Restrict tools to read-only operations.",
    )
    parser.add_argument(
        "--web-search", action=argparse.BooleanOptionalAction, default=None,
        help="Enable Tavily web search (--no-web-search disables it); requires TAVILY_API_KEY.",
    )
    parser.add_argument(
        "--reasoning-effort",
        help="Reasoning effort: default, none, or an official level supported by the model.",
    )
    args = parser.parse_args(argv)

    env = EnvironmentConfig.from_env()
    if args.reasoning_effort is not None:
        env = replace(env, reasoning_effort=args.reasoning_effort)
    if env.reasoning_effort != "default":
        from codeagent.reasoning import environment_capabilities, validate_effort
        try:
            validate_effort(env.reasoning_effort, environment_capabilities(env))
        except ValueError as exc:
            parser.error(str(exc))
    if args.web_search is not None:
        env = replace(env, web_search_config=replace(env.web_search_config, enabled=args.web_search))
    if env.web_search_config.enabled and not env.web_search_config.available:
        parser.error("联网搜索需要配置 TAVILY_API_KEY；也可使用 --no-web-search 关闭。")
    stream = env.stream and not args.no_stream
    workspace = Path.cwd()
    data_paths = RuntimeDataPaths(env.data_dir)
    query = " ".join(args.query).strip()
    requested_backend = args.planning_mode or env.planning_mode
    planning_backend = resolve_planning_backend(
        requested_backend,
        interactive=not bool(query),
    )
    skill_loader = create_skill_loader(env, workspace)
    skill_catalog = skill_loader.catalog_prompt() if skill_loader is not None else ""
    recovery_runtime = RecoveryRuntime(
        env.recovery_config,
        log=print if env.recovery_config.trace else None,
    )
    runtime_repository = (
        SQLiteRepository.for_workspace(
            workspace,
            recover_incomplete=False,
            data_dir=data_paths.root,
        )
        if env.memory_config.enabled or planning_backend is PlanningBackend.TASKS
        else None
    )
    memory_access = (
        MemoryAccessController(runtime_repository.has_active_team_run_for_workspace)
        if runtime_repository is not None
        else None
    )
    memory_store = create_memory_store(
        env,
        workspace,
        data_paths,
        access_policy=(memory_access.policy(workspace) if memory_access else None),
    )
    memory_manager = (
        MemoryManager(
            memory_store,
            env.memory_config,
            recovery_runtime=recovery_runtime,
        )
        if memory_store is not None
        else None
    )
    memory_catalog = (
        memory_manager.catalog_prompt() if memory_manager is not None else ""
    )
    todo_store = TodoStore() if planning_backend is PlanningBackend.TODO else None
    task_repository = None
    task_list_id = None
    task_state_provider = None
    if planning_backend is PlanningBackend.TASKS:
        assert runtime_repository is not None
        task_repository = runtime_repository
        if args.task_list:
            task_list = task_repository.get_task_list(args.task_list)
            if task_list is None:
                parser.error(f"task list not found: {args.task_list}")
            if Path(task_list.workspace).resolve() != workspace.resolve():
                parser.error("task list belongs to another workspace")
        else:
            task_list = task_repository.create_task_list(
                workspace=workspace,
                name="CLI tasks",
            )
        task_list_id = task_list.id
        print(f"Task list: {task_list_id}")

        def task_state() -> str:
            resources = task_repository.list_task_resources(task_list_id)
            return json.dumps(
                [resource.to_dict(camel_case=True) for resource in resources],
                ensure_ascii=False,
                indent=2,
            )

        task_state_provider = task_state
    context_root = data_paths.context_dir(
        workspace,
        conversation_id=task_list_id or "cli",
        agent_id="agent_root",
    )
    context_config = replace(
        env.context_config,
        transcript_dir=context_root / "transcripts",
        tool_output_dir=context_root / "tool-results",
    )
    context = ContextManager(
        config=context_config,
        todo_store=todo_store,
        task_state_provider=task_state_provider,
    )
    prompt_runtime = PromptRuntime(workspace=workspace, config=env.prompt_config)
    client = env.create_anthropic_client(
        stream=stream, on_text=print_stream_token if stream else None,
    )
    tools = create_default_registry(
        web_search_config=env.web_search_config,
        lsp_config=env.lsp_config,
        workspace_guard=WorkspaceGuard(workspace),
        code_search_tool=SearchCodeTool(
            workspace, data_paths.code_index_dir(workspace), client=client, model=env.model_id,
            embedding_config=env.embedding_config,
        ),
        ask_user_fn=terminal_ask_user,
        todo_store=todo_store,
        todo_log=print,
        skill_loader=skill_loader,
        memory_store=memory_store,
        memory_max_items=env.memory_config.max_loaded_items,
        planning_backend=planning_backend,
        task_service=task_repository,
        task_list_id=task_list_id,
    )
    tools.register(LoadToolOutputTool(context_config.tool_output_dir))
    tools.register(LoadContextHistoryTool(context_config.transcript_dir))
    mcp_path = (
        env.mcp_config_path
        if env.mcp_config_path.is_absolute()
        else workspace / env.mcp_config_path
    )
    mcp_router = McpRouter(mcp_path)
    mcp_router.register_tools(tools)
    permission_broker = CliPermissionBroker()

    agent = Agent(
        client=client,
        tools=tools,
        config=env.to_agent_config(planning_backend=planning_backend),
        prompt_mode=PromptMode.NORMAL,
        read_only=args.read_only,
        hooks=create_default_hooks(
            permission_policy=PermissionPolicy(workspace=workspace, broker=permission_broker),
            workspace=workspace,
            todo_store=todo_store,
            planning_backend=planning_backend,
            task_state_provider=task_state_provider,
        ),
        context=context,
        memory_manager=memory_manager,
        prompt_runtime=prompt_runtime,
        prompt_log=print if env.prompt_config.emit_trace else None,
        recovery_runtime=recovery_runtime,
        permission_broker=permission_broker,
        subagent_environment_factory=lambda: create_default_subagent_environment(
            workspace,
            skill_loader,
            memory_store,
            env,
            context_root,
            permission_broker=permission_broker,
        ),
        subagent_log=print,
        skill_catalog=skill_catalog,
        memory_catalog=memory_catalog,
    )

    try:
        if query:
            result = agent.run(query)
            print_run_result(result, stream=stream)
            return 1 if is_execution_failure(result.stop_reason) else 0

        print("codeagent interactive mode. Type /read-only on or /read-only off to change permissions; q, quit, or exit to stop.")
        while True:
            try:
                user_input = input("[read-only] > " if agent.read_only else "> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0

            if user_input.lower() in {"q", "quit", "exit"}:
                return 0
            if not user_input:
                continue
            if user_input.lower() in {"/read-only on", "/read-only off"}:
                enabled = user_input.lower().endswith(" on")
                agent.set_read_only(enabled)
                print("Read-only permission enabled." if enabled else "Normal permissions restored.")
                continue

            result = agent.run(user_input)
            print_run_result(result, stream=stream)
    finally:
        mcp_router.close()
        if runtime_repository is not None:
            runtime_repository.close()


def print_run_result(result: AgentResult, *, stream: bool) -> None:
    if stream:
        print()
    if result.final_text and (not stream or is_execution_failure(result.stop_reason)):
        print(result.final_text)


def print_stream_token(token: str) -> None:
    print(token, end="", flush=True)


def create_skill_loader(env: EnvironmentConfig, workspace: Path) -> SkillLoader | None:
    """Load the shared library; workspace is retained for caller compatibility."""

    if not env.enable_skills:
        return None

    data_paths = RuntimeDataPaths(env.data_dir)
    roots = [data_paths.skill_root(root) for root in env.skill_roots]
    return SkillLoader(roots=roots)


def create_memory_store(
    env: EnvironmentConfig,
    workspace: Path,
    data_paths: RuntimeDataPaths,
    *,
    access_policy: MemoryAccessPolicy | None = None,
) -> MemoryStore | None:
    if not env.memory_config.enabled:
        return None

    root = data_paths.memory_dir(workspace)
    legacy = data_paths.legacy_path(workspace, env.memory_config.memory_dir)
    try:
        if access_policy is None:
            data_paths.import_legacy_directory(legacy, root)
        else:
            with access_policy.writing():
                data_paths.import_legacy_directory(legacy, root)
    except MemoryWriteBlocked:
        pass
    return MemoryStore(
        root=root,
        max_memory_bytes=env.memory_config.max_memory_bytes,
        access_policy=access_policy,
    )


def create_default_subagent_environment(
    workspace: Path,
    skill_loader: SkillLoader | None,
    memory_store: MemoryStore | None,
    env: EnvironmentConfig,
    context_root: Path,
    permission_broker: CliPermissionBroker | None = None,
):
    todo_store = TodoStore()
    subagent_root = context_root / "subagents" / uuid4().hex
    context_config = replace(
        env.context_config,
        transcript_dir=subagent_root / "transcripts",
        tool_output_dir=subagent_root / "tool-results",
    )
    tools = create_default_registry(
        web_search_config=env.web_search_config,
        lsp_config=env.lsp_config,
        workspace_guard=WorkspaceGuard(workspace),
        code_search_tool=SearchCodeTool(workspace, RuntimeDataPaths(env.data_dir).code_index_dir(workspace),
                                        embedding_config=env.embedding_config),
        todo_store=todo_store,
        todo_log=print,
        skill_loader=skill_loader,
        memory_store=memory_store,
        allow_memory_write=env.memory_config.allow_subagent_write,
        memory_max_items=env.memory_config.max_loaded_items,
    )
    tools.register(LoadToolOutputTool(context_config.tool_output_dir))
    tools.register(LoadContextHistoryTool(context_config.transcript_dir))
    context = ContextManager(config=context_config, todo_store=todo_store)
    return (
        tools,
        create_default_hooks(
            workspace=workspace,
            todo_store=todo_store,
            permission_policy=PermissionPolicy(workspace=workspace, broker=permission_broker),
        ),
        context,
    )


if __name__ == "__main__":
    raise SystemExit(main())
