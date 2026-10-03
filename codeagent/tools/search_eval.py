"""Explicit lexical and configured hybrid evaluation arms."""
from codeagent.tools.search_code import SearchCodeTool


def build_tool(*, workspace, index_dir, client, model, event_emitter):
    return SearchCodeTool(workspace, index_dir, client=client, model=model, event_emitter=event_emitter)


def build_hybrid_tool(*, workspace, index_dir, client, model, event_emitter):
    from codeagent.config import embedding_config_from_env
    config = embedding_config_from_env()
    if not config.enabled:
        raise ValueError("Hybrid evaluation requires CODEAGENT_EMBEDDING_ENABLED=true")
    return SearchCodeTool(workspace, index_dir, client=client, model=model,
                          event_emitter=event_emitter, embedding_config=config)


def build_window_tool(*, workspace, index_dir, client, model, event_emitter):
    """Language-agnostic lexical baseline for the SAME multilingual corpus."""
    tool = build_tool(workspace=workspace, index_dir=index_dir, client=client, model=model, event_emitter=event_emitter)
    tool.service.index.analysis_mode = 'text'
    return tool
