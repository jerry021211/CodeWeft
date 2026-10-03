"""Model response structures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from codeagent.events import TokenUsage


@dataclass(slots=True)
class ModelResponse:
    stop_reason: str
    content: Any
    raw: Any | None = None
    usage: TokenUsage | None = None


class ModelClient(Protocol):
    """Provider-independent contract consumed by the Agent loop."""

    def create_message(self, *, model: str, system: Any, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]], max_tokens: int | None = None) -> ModelResponse: ...

    def fork(self, **kwargs: Any) -> ModelClient: ...
