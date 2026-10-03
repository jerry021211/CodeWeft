"""Validated, portable user attachments; binary data never becomes prompt text."""
from __future__ import annotations

import base64
import binascii
import mimetypes
from pathlib import Path
from typing import Any

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 8
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
AUDIO_TYPES = {"audio/wav", "audio/mpeg"}
TEXT_TYPES = {"application/json", "application/xml", "application/csv"}


def validate_attachments(attachments: list[dict[str, Any]]) -> list[dict[str, str]]:
    if len(attachments) > MAX_ATTACHMENTS:
        raise ValueError(f"最多支持 {MAX_ATTACHMENTS} 个附件")
    result = []
    total = 0
    for item in attachments:
        if not isinstance(item, dict) or set(item) - {"name", "media_type", "data"}:
            raise ValueError("附件必须包含 name、media_type 和 base64 data")
        name, media, data = (item.get(key) for key in ("name", "media_type", "data"))
        if not isinstance(name, str) or not name.strip() or len(name) > 255:
            raise ValueError("附件名称必须为 1–255 个字符")
        if not isinstance(media, str) or media not in IMAGE_TYPES | AUDIO_TYPES | TEXT_TYPES | {"application/pdf"} and not media.startswith("text/"):
            raise ValueError("不支持的附件类型；请使用图片、PDF、UTF-8 文本、WAV 或 MP3")
        if not isinstance(data, str) or len(data) > (MAX_ATTACHMENT_BYTES + 2) // 3 * 4:
            raise ValueError("单个附件不能超过 10 MiB")
        try:
            decoded = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("附件 data 必须为有效 base64") from exc
        if not decoded or len(decoded) > MAX_ATTACHMENT_BYTES:
            raise ValueError("附件不能为空或超过 10 MiB")
        total += len(decoded)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("附件总大小不能超过 20 MiB")
        if media.startswith("text/") or media in TEXT_TYPES:
            try:
                decoded.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("文本附件必须为 UTF-8 编码") from exc
        result.append({"name": name, "media_type": media, "data": data})
    return result


def user_content(text: str, attachments: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    attachments = validate_attachments(attachments)
    if not attachments:
        return text
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    for item in attachments:
        media = item["media_type"]
        if media.startswith("text/") or media in TEXT_TYPES:
            decoded = base64.b64decode(item["data"]).decode("utf-8")
            blocks.append({"type": "text", "text": f"附件 {item['name']}：\n{decoded}"})
        else:
            kind = "image" if media in IMAGE_TYPES else "audio" if media in AUDIO_TYPES else "document"
            blocks.append({"type": kind, "title": item["name"], "source": {
                "type": "base64", "media_type": media, "data": item["data"],
            }})
    return blocks


def attachment_from_path(path: str | Path) -> dict[str, str]:
    path = Path(path)
    if path.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise ValueError("单个附件不能超过 10 MiB")
    media = {".csv": "text/csv", ".md": "text/markdown", ".json": "application/json",
             ".wav": "audio/wav", ".mp3": "audio/mpeg", ".pdf": "application/pdf"}.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "text/plain"
    media = {"audio/x-wav": "audio/wav"}.get(media, media)
    return validate_attachments([{"name": path.name, "media_type": media,
                                  "data": base64.b64encode(path.read_bytes()).decode("ascii")}])[0]


def validate_modalities(messages: list[dict], protocol: str, modalities: tuple[str, ...] | None = None) -> None:
    supported = {"text", "image", "document"}
    if protocol == "openai_chat":
        supported.add("audio")
    if modalities is not None:
        supported &= set(modalities)
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            kind = block.get("type")
            if kind in {"image", "document", "audio", "video"} and kind not in supported:
                raise ValueError(f"Input validation: 当前模型/接口 {protocol} 不支持 {kind} 输入")


def anthropic_messages(messages: list[dict]) -> list[dict]:
    """Drop portable-only labels from image blocks without changing history."""
    validate_modalities(messages, "anthropic")
    return [{**message, "content": [
        {key: value for key, value in block.items() if key != "_chat_extra" and (key != "title" or block.get("type") != "image")}
        for block in message["content"] if block.get("type") != "provider_reasoning"
    ] if isinstance(message.get("content"), list) else message.get("content", "")} for message in messages]
