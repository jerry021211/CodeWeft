"""Compatibility exports; analysis is shared across code tools."""
from codeagent.code_intelligence.models import source_text
from codeagent.code_intelligence.languages import analyze as chunks
from codeagent.memory.retrieval import terms


def encoded(text: str) -> str:
    return " ".join("t" + term.encode("utf-8").hex() for term in terms(text))
