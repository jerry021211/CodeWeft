"""Observe whether model message history remains append-only across calls."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from codeagent.messages import Message


def fingerprint(value: Any) -> dict[str, Any]:
    """Canonical client JSON: sorted object keys, ordered arrays, no body logs."""
    from codeagent.context.history import serializable
    body = json.dumps(serializable(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return {"hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "chars": len(body), "bytes": len(body.encode("utf-8"))}


def observe_request(params: dict[str, Any], baselines: dict[str, Any], *,
                    call_kind: str, metadata: dict[str, Any] | None = None,
                    boundary: str = "sdk_parameters") -> dict[str, Any]:
    """Hash the final client parameters; these are NOT provider cache keys.

    Baselines contain only fingerprints and belong to one client/session. Forks
    must get independent baselines even when their call_kind is identical.
    """
    current = {key: fingerprint(params.get(key)) for key in ("system", "tools")}
    current["messages"] = [fingerprint(message) for message in params.get("messages", [])]
    current["configuration"] = fingerprint({key: value for key, value in params.items()
                                             if key not in {"system", "tools", "messages"}})
    previous = baselines.get(call_kind)
    baseline_reset = previous is not None and (not isinstance(previous, dict)
        or previous.get("version") != 1 or not isinstance(previous.get("messages"), list)
        or any(key not in previous for key in ("configuration", "system", "tools")))
    if baseline_reset:
        previous = None
    current["version"] = 1
    common = 0
    first = None
    if previous:
        for old, new in zip(previous["messages"], current["messages"]):
            if old != new:
                break
            common += 1
        for key in ("configuration", "system", "tools"):
            if previous[key] != current[key]:
                first = key
                break
        if first is None and (common < len(previous["messages"]) or common < len(current["messages"])):
            first = f"messages[{common}]"
    append_only = bool(previous and all(previous[k] == current[k] for k in
                       ("configuration", "system", "tools")) and common == len(previous["messages"]))
    baselines[call_kind] = current
    changed_components = [key for key in ("configuration", "system", "tools")
                          if previous and previous[key] != current[key]]
    reasons = list((metadata or {}).get("rewrite_reasons", []))
    reasons.extend(f"request_{key}_changed" for key in changed_components)
    if previous and common < len(previous["messages"]) and not reasons:
        reasons.append("unexpected_message_prefix_change")
    return {"summary_revision": None, "history_generation": None, "prompt_revision": None,
            **(metadata or {}), "rewrite_reasons": list(dict.fromkeys(reasons)),
            "changed_components": changed_components,
            "observation_version": 1, "observation_boundary": boundary,
            "provider_cache_key": False, "model": params.get("model"), "call_kind": call_kind,
            "baseline_reset_reason": "incompatible_snapshot" if baseline_reset else None,
            "request": fingerprint(params), **current,
            "baseline_available": previous is not None, "first_difference": first,
            "common_prefix_messages": common, "append_only": append_only,
            "unchanged": bool(previous == current),
            "history_rewritten": bool(previous and common < len(previous["messages"])),
            "first_changed_message": common if previous and common < len(previous["messages"]) else None}


def emit_request_observation(emit: Any, payload: dict[str, Any]) -> None:
    """Chunk fingerprints below the existing redaction event/array limits."""
    messages = payload["messages"]
    emit("request.observed", {**payload, "messages": messages[:100],
                              "message_count": len(messages), "message_offset": 0})
    for offset in range(100, len(messages), 100):
        emit("request.messages_observed", {
            "call_id": payload.get("call_id"), "request_hash": payload["request"]["hash"],
            "call_kind": payload["call_kind"], "message_offset": offset,
            "messages": messages[offset:offset + 100],
        })


@dataclass(frozen=True, slots=True)
class HistoryObservation:
    generation: int
    rewritten: bool
    rewrite_reason: str | None
    generation_reason: str | None
    previous_message_count: int
    current_message_count: int
    message_count_delta: int
    common_prefix_messages: int
    previous_suffix_messages: int
    current_suffix_messages: int
    previous_history_hash: str | None
    current_history_hash: str

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "history_generation": self.generation,
            "history_rewritten": self.rewritten,
            "rewrite_reason": self.rewrite_reason,
            "generation_reason": self.generation_reason,
            "previous_message_count": self.previous_message_count,
            "current_message_count": self.current_message_count,
            "message_count_delta": self.message_count_delta,
            "common_prefix_messages": self.common_prefix_messages,
            "previous_suffix_messages": self.previous_suffix_messages,
            "current_suffix_messages": self.current_suffix_messages,
            "previous_history_hash": self.previous_history_hash,
            "current_history_hash": self.current_history_hash,
        }


class HistoryObserver:
    """Compare consecutive histories sent to the main model."""

    def __init__(
        self,
        *,
        generation: int = 0,
        last_sent: list[Message] | None = None,
    ) -> None:
        self._generation = generation
        self._last_sent = deepcopy(last_sent) if last_sent is not None else None
        self._last_hash = _history_hash(self._last_sent) if self._last_sent is not None else None

    def restore(self, *, generation: int, last_sent: list[Message]) -> None:
        self._generation = generation
        self._last_sent = deepcopy(last_sent)
        self._last_hash = _history_hash(self._last_sent)

    def observe(
        self,
        messages: list[Message],
        *,
        generation: int | None = None,
        generation_reason: str | None = None,
    ) -> HistoryObservation:
        current = deepcopy(messages)
        current_hash = _history_hash(current)
        previous = self._last_sent

        if previous is None:
            if generation is not None:
                self._generation = generation
            observation = HistoryObservation(
                generation=self._generation,
                rewritten=False,
                rewrite_reason=None,
                generation_reason=None,
                previous_message_count=0,
                current_message_count=len(current),
                message_count_delta=len(current),
                common_prefix_messages=0,
                previous_suffix_messages=0,
                current_suffix_messages=len(current),
                previous_history_hash=None,
                current_history_hash=current_hash,
            )
            self._last_sent = current
            self._last_hash = current_hash
            return observation

        common_prefix = _common_prefix_length(previous, current)
        rewritten = common_prefix < len(previous)
        applied_reason: str | None = None
        if rewritten:
            if generation is not None and generation > self._generation:
                self._generation = generation
                applied_reason = generation_reason or "unexpected_prefix_change"
            else:
                self._generation += 1
                applied_reason = generation_reason or "unexpected_prefix_change"

        observation = HistoryObservation(
            generation=self._generation,
            rewritten=rewritten,
            rewrite_reason=applied_reason,
            generation_reason=applied_reason,
            previous_message_count=len(previous),
            current_message_count=len(current),
            message_count_delta=len(current) - len(previous),
            common_prefix_messages=common_prefix,
            previous_suffix_messages=max(0, len(previous) - common_prefix),
            current_suffix_messages=max(0, len(current) - common_prefix),
            previous_history_hash=self._last_hash,
            current_history_hash=current_hash,
        )
        self._last_sent = current
        self._last_hash = current_hash
        return observation


def _common_prefix_length(previous: list[Message], current: list[Message]) -> int:
    length = 0
    for old_message, new_message in zip(previous, current):
        if old_message != new_message:
            break
        length += 1
    return length


def _history_hash(messages: list[Message]) -> str:
    serialized = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


__all__ = ["HistoryObservation", "HistoryObserver"]
