"""Evaluation adapter: exactly the same search tool as CLI and Web."""
from codeagent.tools.search_code import SearchCodeTool


def build_tool(*, workspace, index_dir, client, model, event_emitter):
    return SearchCodeTool(workspace, index_dir, client=client, model=model, event_emitter=event_emitter)
