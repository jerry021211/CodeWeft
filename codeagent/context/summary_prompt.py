"""Dedicated summary prompts, using the existing Markdown / str.format convention."""
from codeagent.prompts.template import load_template


SUMMARIZATION_SYSTEM_PROMPT = load_template("context_summary_system")
SUMMARY_FORMAT = load_template("context_summary_format")
_INITIAL = load_template("context_summary_initial")
_UPDATE = load_template("context_summary_update")
_HANDOFF = load_template("context_summary_handoff")


def summary_user_prompt(*, previous_summary: str, conversation: str,
                        summary_char_budget: int) -> str:
    template = _UPDATE if previous_summary.strip() else _INITIAL
    # One substitution pass: braces in conversation/old-summary data stay literal.
    return template.format(previous_summary=previous_summary, conversation=conversation,
                           summary_char_budget=summary_char_budget,
                           summary_format=SUMMARY_FORMAT)


def summary_handoff(*, summary: str, revision: int, transcript: str) -> str:
    return _HANDOFF.format(summary=summary, revision=revision, transcript=transcript)
