"""Persisted model services, layered over environment defaults.

Only model settings live here. Credentials stay in the local runtime data
directory and are never returned by the public settings representation.
"""
from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from codeagent.model_metadata import models_url


def settings_path(data_dir: Path) -> Path:
    return Path(data_dir) / "settings" / "models.json"


def read_settings(data_dir: Path) -> dict | None:
    path = settings_path(data_dir)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("version") != 1 or not isinstance(value.get("revision"), str):
            raise ValueError("Unknown settings version")
        validate_settings(value["services"])
        return value
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise RuntimeError("本机 models.json 模型配置无效，请检查配置文件。") from exc


def export_settings(env) -> dict:
    return {
        "chat": {"protocol": env.model_protocol, "model": env.model_id,
                 "base_url": env.base_url or ("https://api.anthropic.com" if env.model_protocol == "anthropic" else "https://api.openai.com/v1"),
                 "api_key": env.api_key or "", "max_tokens": env.max_tokens, "stream": env.stream,
                 "input_modalities": list(env.input_modalities) if env.input_modalities is not None else None,
                 "reasoning_levels": list(env.reasoning_levels), "reasoning_effort": env.reasoning_effort,
                 "token_parameter": env.chat_token_parameter},
        "speech": {"enabled": env.speech_config.enabled, "model": env.speech_config.model,
                   "base_url": env.speech_config.base_url, "api_key": env.speech_config.api_key,
                   "language": env.speech_config.language or "", "timeout_seconds": env.speech_config.timeout_seconds},
        "embedding": {"enabled": env.embedding_config.enabled, "model": env.embedding_config.model,
                      "base_url": env.embedding_config.base_url, "api_key": env.embedding_config.api_key,
                      "dimensions": env.embedding_config.dimensions, "timeout_seconds": env.embedding_config.timeout_seconds,
                      "batch_size": env.embedding_config.batch_size},
    }


def public_settings(env, revision: str) -> dict:
    services = export_settings(env)
    for service in services.values():
        service["has_api_key"] = bool(service.pop("api_key"))
    return {"revision": revision, "services": services, "configured": bool(env.model_id)}


def validate_endpoint(value: str, *, required: bool = True) -> None:
    if not value and not required:
        return
    parts = urlsplit(value)
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("服务地址端口无效") from exc
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or any(char.isspace() for char in value)):
        raise ValueError("服务地址必须为 http(s) URL，不能包含账号密码、查询参数或片段。")


def validate_settings(services: dict) -> None:
    if set(services) != {"chat", "speech", "embedding"}:
        raise ValueError("需要完整的对话、语音和向量配置")
    for name, service in services.items():
        enabled = name == "chat" or service["enabled"]
        if not isinstance(service["model"], str) or enabled and not service["model"].strip():
            raise ValueError(f"{name}：请填写模型名称")
        validate_endpoint(service["base_url"], required=enabled)
        if not isinstance(service["api_key"], str) or "\n" in service["api_key"] or "\r" in service["api_key"]:
            raise ValueError("API Key 格式无效")
    chat = services["chat"]
    if chat["protocol"] not in {"anthropic", "openai_chat", "openai_responses"}:
        raise ValueError("不支持的对话接口协议")
    if chat["token_parameter"] not in {"max_completion_tokens", "max_tokens"}:
        raise ValueError("不支持的输出上限参数")
    if type(chat["max_tokens"]) is not int or not 1 <= chat["max_tokens"] <= 2_000_000:
        raise ValueError("输出 Token 上限必须为 1–2000000 的整数")
    modalities = chat["input_modalities"]
    if modalities is not None and (not isinstance(modalities, list) or not modalities or any(value not in {"text", "image", "document", "audio"} for value in modalities)):
        raise ValueError("模型输入能力配置无效")
    levels = chat["reasoning_levels"]
    if not isinstance(levels, list) or any(not isinstance(level, str) or not level or len(level) > 32 or level == "default" for level in levels):
        raise ValueError("推理等级配置无效")
    if not isinstance(chat["reasoning_effort"], str) or not chat["reasoning_effort"]:
        raise ValueError("默认推理等级无效")
    if chat["protocol"] != "anthropic" and chat["reasoning_effort"] not in ["default", *levels]:
        raise ValueError("默认推理等级必须包含在支持的等级中")
    for name in ("speech", "embedding"):
        service = services[name]
        if type(service["enabled"]) is not bool:
            raise ValueError("启用状态必须为布尔值")
        if type(service["timeout_seconds"]) not in {int, float} or not 0 < service["timeout_seconds"] <= 600:
            raise ValueError("超时必须在 0–600 秒之间")
    embedding = services["embedding"]
    if type(embedding["dimensions"]) is not int or not 0 <= embedding["dimensions"] <= 65536 or embedding["enabled"] and embedding["dimensions"] == 0:
        raise ValueError("启用向量模型时必须填写实际输出维度（1–65536）")
    if type(embedding["batch_size"]) is not int or not 1 <= embedding["batch_size"] <= 2048:
        raise ValueError("向量批量大小必须为 1–2048")
    if type(chat["stream"]) is not bool:
        raise ValueError("流式输出必须为布尔值")


def resolve_keys(services: dict, env) -> dict:
    """null retains a secret; empty string explicitly clears it."""
    result = deepcopy(services)
    current = export_settings(env)
    for name, service in result.items():
        if service.get("api_key") is None:
            old = current[name]
            if old["api_key"] and service["base_url"].rstrip("/") != old["base_url"].rstrip("/"):
                raise ValueError(f"{name}：服务地址已更改，请重新填写密钥，或选择清除密钥。")
            service["api_key"] = old["api_key"]
    return result


def apply_settings(env, services: dict):
    validate_settings(services)
    chat, speech, embedding = (services[name] for name in ("chat", "speech", "embedding"))
    return replace(env, model_id=chat["model"].strip(), model_protocol=chat["protocol"],
                   base_url=chat["base_url"].rstrip("/"), api_key=chat["api_key"] or None,
                   max_tokens=chat["max_tokens"], stream=chat["stream"],
                   input_modalities=tuple(chat["input_modalities"]) if chat["input_modalities"] is not None else None,
                   reasoning_levels=tuple(chat["reasoning_levels"]), reasoning_effort=chat["reasoning_effort"],
                   chat_token_parameter=chat["token_parameter"],
                   context_config=replace(env.context_config, summarization_model=chat["model"].strip(), summarization_api_key=None),
                   recovery_config=replace(env.recovery_config, fallback_model=""),
                   speech_config=replace(env.speech_config, **{**speech, "language": speech["language"] or None}),
                   embedding_config=replace(env.embedding_config, **embedding))


def write_settings(data_dir: Path, services: dict, *, expected_revision: str) -> str:
    validate_settings(services)
    current = read_settings(data_dir)
    if (current["revision"] if current else "environment") != expected_revision:
        raise SettingsConflict("配置已在其他页面更新，请重新读取后再保存。")
    path = settings_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    revision = uuid4().hex
    fd, temp = tempfile.mkstemp(prefix=".models-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"version": 1, "revision": revision, "services": services}, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return revision


class SettingsConflict(ValueError):
    pass


def discover_models(service: dict, *, protocol: str = "openai_chat") -> list[str]:
    validate_endpoint(service["base_url"])
    url = models_url(service["base_url"], protocol=protocol)
    headers = {}
    if service["api_key"]:
        headers = {"x-api-key": service["api_key"], "anthropic-version": "2023-06-01"} if protocol == "anthropic" else {"Authorization": f"Bearer {service['api_key']}"}
    elif protocol == "anthropic":
        headers = {"anthropic-version": "2023-06-01"}
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            with client.stream("GET", url, headers=headers) as response:
                if not response.is_success:
                    raise ValueError(f"服务返回 HTTP {response.status_code}；请检查地址、密钥和 /models 支持情况。")
                data = bytearray()
                for part in response.iter_bytes():
                    data.extend(part)
                    if len(data) > 2_000_000:
                        raise ValueError("模型列表响应过大")
        rows = json.loads(data).get("data")
        if not isinstance(rows, list):
            raise ValueError("此服务未返回标准模型列表，请手动填写模型名称。")
        return sorted({row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str) and 0 < len(row["id"]) <= 256})[:1000]
    except httpx.HTTPError as exc:
        raise ValueError("无法连接模型服务，请检查服务地址、网络和超时。") from exc
    except (json.JSONDecodeError, AttributeError, UnicodeDecodeError) as exc:
        raise ValueError("模型服务没有返回有效 JSON 列表，请手动填写模型名称。") from exc
