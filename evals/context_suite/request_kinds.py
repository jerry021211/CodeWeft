"""Recognize recorded summary requests across prompt generations."""

_SUMMARY_PREFIXES = (
    "你是上下文摘要助手。\n",
    "你在压缩一段多轮对话的早期历史，为后续轮次保留可靠的「记忆」。",
    "你是编程助手的上下文摘要器，只生成供后续继续工作的结构化 Markdown 摘要。",
)


def is_summary_request(request: dict) -> bool:
    system = request.get("system")
    return isinstance(system, str) and system.startswith(_SUMMARY_PREFIXES)
