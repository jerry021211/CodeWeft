"""Content-free metrics for the request view; never mutate canonical history."""

from codeagent.messages import Message, _field


def tool_projection_metrics(canonical: list[Message], projected: list[Message]) -> dict[str, int]:
    """Count shortened retained results, excluding history folded into summaries.

    Character savings describe the current request, not cumulative savings across
    calls. Only plain text results are counted, so media is never misreported.
    """
    def results(messages: list[Message]) -> dict[str, str]:
        found = {}
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if _field(block, "type") != "tool_result":
                    continue
                key, text = _field(block, "tool_use_id"), _field(block, "content")
                if isinstance(key, str) and isinstance(text, str):
                    found[key] = text
        return found

    before = results(canonical)
    count = saved = 0
    for key, text in results(projected).items():
        original = before.get(key)
        if original is not None and len(original) > len(text):
            count += 1
            saved += len(original) - len(text)
    return {"projected_tool_results": count, "tool_result_chars_saved": saved}
