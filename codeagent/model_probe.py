"""Small, explicit model calls for the settings page; no retries or persistence."""
from __future__ import annotations

import io
import json
import math
import time
import wave

import httpx

from codeagent.model_settings import validate_endpoint
from codeagent.providers import request_payload


def probe_model(name: str, service: dict) -> dict:
    validate_endpoint(service["base_url"])
    model = service["model"].strip()
    if not model:
        raise ValueError("请先填写要测试的模型名称。")
    key = service["api_key"]
    if not isinstance(key, str) or any(char in key for char in "\r\n"):
        raise ValueError("API Key 格式无效")
    base = service["base_url"].rstrip("/")
    protocol = service.get("protocol", "openai_chat")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    options = {}
    if name == "chat":
        messages = [{"role": "user", "content": "Reply with OK."}]
        tokens = min(service["max_tokens"], 256)
        if protocol == "anthropic":
            url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
            headers = {"anthropic-version": "2023-06-01", **({"x-api-key": key} if key else {})}
            payload = {"model": model, "messages": messages, "max_tokens": tokens, "stream": False}
        else:
            url = base + ("/responses" if protocol == "openai_responses" else "/chat/completions")
            payload = request_payload(protocol, model=model, system="", messages=messages, tools=[],
                                      max_tokens=tokens, reasoning_effort=service["reasoning_effort"],
                                      token_parameter=service["token_parameter"])
            payload["stream"] = False
        options = {"json": payload}
    elif name == "embedding":
        url = base + "/embeddings"
        # The runtime validates native output dimensions; it does not request projection.
        options = {"json": {"model": model, "input": ["connection test"], "encoding_format": "float"}}
    elif name == "speech":
        url = base + "/audio/transcriptions"
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\x00\x00" * 16000)
        data = {"model": model, "response_format": "json"}
        if service.get("language"):
            data["language"] = service["language"]
        options = {"data": data, "files": {"file": ("connection-test.wav", audio.getvalue(), "audio/wav")}}
    else:
        raise ValueError("不支持的模型能力")

    started = time.monotonic()
    timeout = min(float(service.get("timeout_seconds", 30)), 30)
    def result(ok: bool, message: str, **extra) -> dict:
        return {"ok": ok, "message": message, "elapsed_ms": round((time.monotonic() - started) * 1000), **extra}

    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            with client.stream("POST", url, headers=headers, **options) as response:
                if not response.is_success:
                    reasons = {401: "认证失败，请检查 API Key", 403: "无权访问，请检查模型权限或服务访问限制",
                               404: "接口或模型不存在，请检查协议、地址和模型名称", 429: "请求受限，请检查额度或稍后重试"}
                    reason = reasons.get(response.status_code, "服务拒绝请求，请检查模型与接口配置")
                    return result(False, f"HTTP {response.status_code}：{reason}。", status_code=response.status_code)
                raw = bytearray()
                for part in response.iter_bytes():
                    if time.monotonic() - started > timeout:
                        return result(False, f"测试超时（{timeout:g} 秒），请稍后重试。")
                    raw.extend(part)
                    if len(raw) > 2_000_000:
                        return result(False, "测试响应过大，无法验证结果。")
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get("error"):
            return result(False, "模型返回错误或无效响应，请检查模型和接口配置。")
        if name == "embedding":
            rows = body.get("data")
            vector = rows[0].get("embedding") if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else None
            if not isinstance(vector, list) or not vector or any(type(value) not in {int, float} or not math.isfinite(value) for value in vector):
                return result(False, "向量接口未返回有效的数值向量。")
            actual = len(vector)
            if service["dimensions"] != actual:
                return result(False, f"接口已响应，但返回 {actual} 维，与配置的 {service['dimensions']} 维不符；请修改向量维度。", dimensions=actual)
            return result(True, f"连接成功，已生成 {actual} 维测试向量。", dimensions=actual)
        if name == "speech":
            if not isinstance(body.get("text"), str):
                return result(False, "语音接口未返回有效转写响应。")
            return result(True, "连接成功，语音接口已处理 1 秒静音样本；识别质量需使用真实音频验证。")
        if protocol == "anthropic":
            valid = body.get("type") == "message" and isinstance(body.get("content"), list)
            has_text = valid and any(isinstance(part, dict) and part.get("type") == "text" and part.get("text") for part in body["content"])
        elif protocol == "openai_responses":
            valid = isinstance(body.get("output"), list) and body.get("status") in {"completed", "incomplete"}
            has_text = valid and any(isinstance(item, dict) and isinstance(item.get("content"), list) and any(isinstance(part, dict) and part.get("type") == "output_text" and part.get("text") for part in item["content"]) for item in body["output"])
        else:
            choices = body.get("choices")
            valid = isinstance(choices, list) and bool(choices) and isinstance(choices[0], dict) and isinstance(choices[0].get("message"), dict)
            has_text = valid and bool(choices[0]["message"].get("content"))
        if not valid:
            return result(False, "接口未返回当前协议要求的模型响应，请检查所选协议。")
        if not has_text:
            return result(False, "接口已响应，但测试未获得文本输出；推理模型可能需要更大的输出预算。")
        return result(True, "连接成功，已收到模型回复。此测试验证基本对话，不验证工具调用或多模态能力。")
    except httpx.TimeoutException:
        return result(False, f"测试超时（{timeout:g} 秒），请检查服务或网络。")
    except httpx.HTTPError:
        return result(False, "无法连接模型服务，请检查地址、网络或证书。")
    except (ValueError, UnicodeDecodeError):
        return result(False, "服务没有返回有效的 JSON 响应，请检查接口协议和地址。")
