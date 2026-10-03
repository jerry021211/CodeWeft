"""Lossless, session-local references over the effective request, not raw history.

Every read still hits the filesystem. Only verified repeated bodies in the
outgoing view are replaced; canonical messages are never modified. Rebuilding
after compaction automatically expands a surviving read if its anchor is gone.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from typing import Any

from codeagent.context.observation import fingerprint
from codeagent.context.projection import READ_REFERENCE_MARKER
from codeagent.messages import Message, ToolUse, _field


def record_read(state: dict[str, Any], tool: ToolUse, output: str) -> dict[str, Any]:
    """Record execution evidence only; never register body text or a read hit."""
    if tool.name != "read_file":
        return state
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("receipts"), dict):
        state = {"version": 1, "receipts": {}}
    evidence = getattr(output, "file_read_snapshot", None)
    if getattr(output, "status", None) != "success" or not _valid(evidence):
        state["receipts"].pop(tool.id, None)
        return state
    state["receipts"][tool.id] = {
        **deepcopy(evidence), "arguments_hash": fingerprint(tool.input)["hash"],
    }
    return state


def _valid(evidence: Any) -> bool:
    return (isinstance(evidence, dict) and type(evidence.get("version")) is int and evidence["version"] == 1
            and all(isinstance(evidence.get(key), str) and evidence[key]
                    for key in ("path", "file_hash", "output_hash"))
            and all(type(evidence.get(key)) is int and evidence[key] > 0 for key in ("offset", "limit"))
            and type(evidence.get("force_full")) is bool
            and isinstance(evidence.get("file_stamp"), list) and len(evidence["file_stamp"]) == 5
            and all(type(value) is int for value in evidence["file_stamp"]))


def project_read_references(messages: list[Message], state: dict[str, Any]) -> tuple[list[Message], dict[str, int]]:
    metrics = {"read_reference_count": 0, "read_reference_chars_saved": 0}
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("receipts"), dict):
        return messages, metrics
    receipts = state["receipts"]
    calls = {}
    call_counts: Counter[str] = Counter()
    result_counts: Counter[str] = Counter()
    for message in messages:
        blocks = message.get("content")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if message.get("role") == "assistant" and _field(block, "type") == "tool_use":
                key = _field(block, "id")
                if isinstance(key, str):
                    calls[key] = block
                    call_counts[key] += 1
            elif message.get("role") == "user" and _field(block, "type") == "tool_result":
                key = _field(block, "tool_use_id")
                if isinstance(key, str):
                    result_counts[key] += 1

    projected = messages
    # Anchors only enter this map when their exact original body is present in
    # THIS view. A summary, cleared result, archive preview or reference cannot
    # become an anchor, regardless of what receipts/checkpoints claim.
    anchors: dict[str, str] = {}
    for index, message in enumerate(messages):
        if message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        replacement = None
        for block_index, block in enumerate(message["content"]):
            if not isinstance(block, dict) or block.get("type") != "tool_result" or block.get("is_error"):
                continue
            call_id, text = block.get("tool_use_id"), block.get("content")
            if not isinstance(call_id, str) or not isinstance(text, str):
                continue
            call, receipt = calls.get(call_id), receipts.get(call_id)
            arguments = _field(call, "input")
            if (call_counts[call_id] != 1 or result_counts[call_id] != 1
                    or _field(call, "name") != "read_file" or not _valid(receipt)
                    or not isinstance(arguments, dict)
                    or receipt.get("arguments_hash") != fingerprint(arguments)["hash"]
                    or any(receipt[key] != arguments.get(key, default)
                           for key, default in (("offset", 1), ("limit", 2000), ("force_full", False)))
                    or receipt["output_hash"] != hashlib.sha256(text.encode("utf-8")).hexdigest()):
                continue
            identity = fingerprint({key: receipt[key] for key in
                                    ("path", "file_hash", "file_stamp", "offset", "limit", "output_hash")})["hash"]
            source = anchors.get(identity)
            if source is None:
                anchors[identity] = call_id
                continue
            if receipt["force_full"]:
                continue
            reference = (
                f"{READ_REFERENCE_MARKER}\n"
                "文件字节及读取范围已核对，未变化；原文仍在本次上下文中。\n"
                f"source_tool_use_id: {json.dumps(source, ensure_ascii=False)}\n"
                f"offset: {receipt['offset']}; limit: {receipt['limit']}\n"
                "如需重新展开此范围，使用 read_file 并设置 force_full=true。\n"
                "[/codeagent:file-read-reference:v1]"
            )
            if (len(text) - len(reference) < 256
                    or len(text.encode("utf-8")) - len(reference.encode("utf-8")) < 256):
                continue
            if projected is messages:
                projected = list(messages)
            if replacement is None:
                replacement = {**message, "content": list(message["content"])}
                projected[index] = replacement
            replacement["content"][block_index] = {**block, "content": reference}
            metrics["read_reference_count"] += 1
            metrics["read_reference_chars_saved"] += len(text) - len(reference)
    return projected, metrics
