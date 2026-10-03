"""Wire adapters for OpenAI Chat Completions and Responses.

The loop keeps its existing text/tool_use/tool_result contract. Provider-owned
reasoning items are preserved separately for replay, never emitted as UI text.
"""
from __future__ import annotations

import json
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx

from codeagent.anthropic_client import AnthropicModelClient
from codeagent.context.observation import emit_request_observation, observe_request
from codeagent.events import TokenUsage
from codeagent.messages import extract_text
from codeagent.models import ModelResponse
from codeagent.multimodal import validate_modalities
from codeagent.tracing import trace_run


def _url(block: dict) -> str:
    source = block["source"]
    if source.get("type") == "url":
        return source["url"]
    if source.get("type") == "base64":
        return f"data:{source['media_type']};base64,{source['data']}"
    raise ValueError("Unsupported media source")


def _part(block: dict, *, responses: bool, assistant: bool = False) -> dict:
    kind = block["type"]
    if kind == "text":
        return {"type": ("output_text" if assistant else "input_text") if responses else "text", "text": block["text"],
                **({"annotations": []} if responses and assistant else {})}
    if kind == "image":
        return {"type": "input_image", "image_url": _url(block)} if responses else {"type": "image_url", "image_url": {"url": _url(block)}}
    if kind == "document":
        if block["source"].get("type") == "url":
            if not responses:
                raise ValueError("Chat Completions PDF 输入需要 base64 数据")
            return {"type": "input_file", "file_url": _url(block)}
        file = {"filename": block.get("title", "document.pdf"), "file_data": _url(block)}
        return {"type": "input_file", **file} if responses else {"type": "file", "file": file}
    if kind == "audio" and not responses:
        source = block["source"]
        if source.get("type") != "base64" or source.get("media_type") not in {"audio/wav", "audio/mpeg"}:
            raise ValueError("音频输入需要 base64 WAV 或 MP3")
        return {"type": "input_audio", "input_audio": {"data": source["data"], "format": "wav" if source["media_type"] == "audio/wav" else "mp3"}}
    raise ValueError(f"Unsupported content block: {kind}")


def request_payload(protocol: str, *, model: str, system: Any, messages: list[dict], tools: list[dict],
                    max_tokens: int | None, reasoning_effort: str = "default", token_parameter: str = "max_completion_tokens") -> dict:
    responses = protocol == "openai_responses"
    validate_modalities(messages, protocol)
    history: list[dict] = []
    if system and not responses:
        history.append({"role": "system", "content": system if isinstance(system, str) else extract_text(system)})
    for message in messages:
        role, content = message["role"], message.get("content", "")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        parts, calls, extras = [], [], {}
        # Tool results precede any user text in the same canonical message.
        for block in blocks:
            kind = block["type"]
            if kind == "tool_result":
                output = block.get("content", "")
                if isinstance(output, list):
                    if any(part.get("type") != "text" for part in output):
                        raise ValueError("此适配器的工具结果目前仅支持文本")
                    output = extract_text(output)
                if responses:
                    history.append({"type": "function_call_output", "call_id": block["tool_use_id"], "output": output})
                else:
                    history.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": output})
            elif kind == "tool_use":
                function = {"name": block["name"], "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)}
                if responses:
                    calls.append({"type": "function_call", "call_id": block["id"], **function})
                else:
                    call = {"id": block["id"], "type": "function", "function": function}
                    if block.get("_chat_extra"):
                        call["extra_content"] = block["_chat_extra"]
                    calls.append(call)
            elif kind == "provider_reasoning":
                if responses and block.get("provider") == "openai_responses":
                    history.append(block["item"])
                elif not responses and block.get("provider") == "openai_chat":
                    extras.update(block["item"])
            elif kind in {"thinking", "redacted_thinking"}:
                continue  # Foreign provider reasoning is not portable.
            else:
                parts.append(_part(block, responses=responses, assistant=role == "assistant"))
        if responses:
            if parts:
                history.append({"role": role, "content": parts})
            history.extend(calls)
        elif parts or calls or extras:
            history.append({"role": role, "content": parts or None, **({"tool_calls": calls} if calls else {}), **extras})
    payload: dict[str, Any] = {"model": model, "input" if responses else "messages": history}
    if responses:
        payload.update(instructions=system if isinstance(system, str) else extract_text(system), store=False,
                       include=["reasoning.encrypted_content"])
    if tools:
        functions = [{"name": tool["name"], "description": tool.get("description", ""), "parameters": tool["input_schema"]} for tool in tools]
        payload["tools"] = [{"type": "function", **function, "strict": False} if responses else {"type": "function", "function": function} for function in functions]
    if max_tokens is not None:
        payload["max_output_tokens" if responses else token_parameter] = max_tokens
    if reasoning_effort != "default":
        if responses:
            payload["reasoning"] = {"effort": reasoning_effort}
        else:
            payload["reasoning_effort"] = reasoning_effort
    return payload


def normalize_response(raw: dict, protocol: str, model: str, call_kind: str) -> ModelResponse:
    blocks: list[dict] = []
    if protocol == "openai_responses":
        if raw.get("status") in {"failed", "cancelled"} or raw.get("error"):
            raise RuntimeError(f"Model response failed: {raw.get('error') or raw.get('status')}")
        for item in raw.get("output", []):
            if item["type"] == "message":
                for part in item.get("content", []):
                    if part["type"] in {"output_text", "refusal"}:
                        blocks.append({"type": "text", "text": part.get("text", part.get("refusal", ""))})
            elif item["type"] == "function_call":
                blocks.append(_tool(item["call_id"], item["name"], item["arguments"]))
            elif item["type"] == "reasoning":
                blocks.append({"type": "provider_reasoning", "provider": protocol, "item": item})
        reason = "max_tokens" if (raw.get("incomplete_details") or {}).get("reason") == "max_output_tokens" else "end_turn"
        usage = raw.get("usage")
        input_key, output_key, details_key = "input_tokens", "output_tokens", "input_tokens_details"
    else:
        if not raw.get("choices"):
            raise ValueError("Invalid Chat Completions response: missing choices")
        choice = raw["choices"][0]
        message = choice["message"]
        if message.get("content"):
            blocks.append({"type": "text", "text": message["content"]})
        if message.get("refusal"):
            blocks.append({"type": "text", "text": message["refusal"]})
        if message.get("reasoning_content"):
            blocks.append({"type": "provider_reasoning", "provider": protocol,
                           "item": {"reasoning_content": message["reasoning_content"]}})
        for call in message.get("tool_calls") or []:
            block = _tool(call["id"], call["function"]["name"], call["function"]["arguments"])
            if call.get("extra_content"):
                block["_chat_extra"] = call["extra_content"]
            blocks.append(block)
        reason = "max_tokens" if choice.get("finish_reason") == "length" else "end_turn"
        usage = raw.get("usage")
        input_key, output_key, details_key = "prompt_tokens", "completion_tokens", "prompt_tokens_details"
    if reason != "max_tokens" and any(block["type"] == "tool_use" for block in blocks):
        reason = "tool_use"
    tokens = TokenUsage(model=raw.get("model", model), call_kind=call_kind, provider=protocol, available=False)
    if usage is not None:
        prompt = max(0, int(usage.get(input_key, 0)))
        cached = min(prompt, max(0, int((usage.get(details_key) or {}).get("cached_tokens", usage.get("prompt_cache_hit_tokens", 0)))))
        tokens = TokenUsage(input_tokens=prompt - cached, output_tokens=max(0, int(usage.get(output_key, 0))),
                            cache_read_input_tokens=cached, model=raw.get("model", model), call_kind=call_kind, provider=protocol)
    return ModelResponse(stop_reason=reason, content=blocks, usage=tokens, raw=raw)


def _tool(identifier: str, name: str, arguments: str) -> dict:
    if not isinstance(identifier, str) or not identifier or not isinstance(name, str) or not name:
        raise ValueError("Tool response validation failed: missing id or name")
    value = json.loads(arguments or "{}")
    if not isinstance(value, dict):
        raise ValueError("Tool response validation failed: expected JSON object")
    return {"type": "tool_use", "id": identifier, "name": name, "input": value}


@dataclass(slots=True)
class OpenAIModelClient(AnthropicModelClient):
    """Reuse runtime event/admission helpers, with independent HTTP transports."""
    protocol: str = "openai_chat"
    token_parameter: str = "max_completion_tokens"

    def __post_init__(self) -> None:
        if self.protocol not in {"openai_chat", "openai_responses"}:
            raise ValueError(f"Unknown protocol: {self.protocol}")
        self.base_url = self.base_url or "https://api.openai.com/v1"
        # A scoped client per request avoids leaking sockets in short-lived Agent wrappers.
        self._client = self.sdk_client

    def get_cache_capabilities(self, model: str) -> dict:
        return {"cheap_prefix_reads": False}

    def get_model_window(self, model: str) -> dict:
        from codeagent.model_metadata import model_windows
        return model_windows.resolve(base_url=self.base_url, api_key=self.api_key or "", model=model, protocol=self.protocol)

    def fork(self, **kwargs: Any) -> OpenAIModelClient:
        kind = kwargs.get("call_kind") or self.call_kind
        return OpenAIModelClient(api_key=self.api_key, base_url=self.base_url, protocol=self.protocol,
            input_modalities=self.input_modalities,
            token_parameter=self.token_parameter, sdk_client=self.sdk_client, activity=self.activity,
            request_timeout=self.request_timeout, reasoning_effort=self.reasoning_effort if kind in {"main", "subagent"} else "default",
            stream=self.stream if kwargs.get("stream") is None else kwargs["stream"], on_text=kwargs.get("on_text"),
            event_emitter=kwargs.get("event_emitter") if kwargs.get("event_emitter") is not None else self.event_emitter,
            usage_tracker=kwargs.get("usage_tracker") if kwargs.get("usage_tracker") is not None else self.usage_tracker, call_kind=kind)

    def _create_message(self, *, model: str, system: Any, messages: list[dict], tools: list[dict], max_tokens: int | None = None) -> ModelResponse:
        try:
            validate_modalities(messages, self.protocol, self.input_modalities)
            payload = request_payload(self.protocol, model=model, system=system, messages=messages, tools=tools,
                max_tokens=max_tokens, reasoning_effort=self.reasoning_effort, token_parameter=self.token_parameter)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"Model request validation failed: {exc}") from exc
        payload["stream"] = self.stream
        if self.stream and self.protocol == "openai_chat":
            payload["stream_options"] = {"include_usage": True}
        call_id, started = f"call_{uuid4().hex}", time.monotonic()
        info = {"call_id": call_id, "model": model, "call_kind": self.call_kind, "provider": self.protocol}
        self._emit("model.started", {**info, "max_tokens": max_tokens, "reasoning_effort": self.reasoning_effort,
                    "message_count": len(messages), "tool_count": len(tools), "streaming": self.stream})
        observed = dict(payload)
        observed["system"] = observed.pop("instructions", None)
        observed["messages"] = observed.pop("input", observed.get("messages", []))
        observation = observe_request(observed, self._request_baselines, call_kind=self.call_kind,
                                      metadata=self._request_metadata, boundary=f"{self.protocol}_payload")
        emit_request_observation(self._emit, {**observation, "call_id": call_id})
        timeout = self.request_timeout or 600.0
        if self.activity is not None:
            timeout = min(timeout, self.activity.request_timeout())
        url = self.base_url.rstrip("/") + ("/responses" if self.protocol == "openai_responses" else "/chat/completions")
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        with trace_run(f"llm.{self.protocol}.create_message", run_type="llm", inputs=payload, metadata=info) as trace, (
            self.activity.model_request() if self.activity is not None else nullcontext()
        ):
            try:
                with nullcontext(self._client) if self._client is not None else httpx.Client() as client:
                    if self.stream:
                        with client.stream("POST", url, json=payload, headers=headers, timeout=timeout) as response:
                            if not response.is_success:
                                response.read()
                                _raise_status(response)
                            if self.activity is not None:
                                self.activity.set_request_closer(response.close)
                            try:
                                raw = self._read_stream(response, call_id)
                            finally:
                                if self.activity is not None:
                                    self.activity.set_request_closer(None)
                    else:
                        response = client.post(url, json=payload, headers=headers, timeout=timeout)
                        _raise_status(response)
                        raw = response.json()
                if self.activity is not None:
                    self.activity.check()
                result = normalize_response(raw, self.protocol, model, self.call_kind)
            except Exception as exc:
                if self.activity is not None:
                    try:
                        self.activity.check()
                    except Exception as cancellation:
                        exc = cancellation
                self._emit("model.failed", {**info, "error_type": type(exc).__name__, "error": str(exc),
                                           "duration_ms": round((time.monotonic() - started) * 1000)})
                raise exc
            if self.usage_tracker is not None:
                self.usage_tracker.record(result.usage)
            usage = result.usage.to_dict()
            self._emit("model.completed", {**info, "stop_reason": result.stop_reason, "usage": usage,
                                          "duration_ms": round((time.monotonic() - started) * 1000)})
            self._emit("usage.updated", {**info, **usage})
            trace.end(outputs={"stop_reason": result.stop_reason, "content": result.content, "usage": usage})
            return result

    def _read_stream(self, response: httpx.Response, call_id: str) -> dict:
        message: dict = {"content": "", "tool_calls": []}
        calls: dict[int, dict] = {}
        raw: dict = {"choices": [{"message": message, "finish_reason": None}]}
        completed = False
        for event in _sse(response):
            if self.activity is not None:
                self.activity.check()
            if event.get("error") or event.get("type") in {"error", "response.failed"}:
                raise RuntimeError(f"Model stream failed: {event.get('error') or event.get('response', {}).get('error') or event.get('message')}")
            if self.protocol == "openai_responses":
                kind = event.get("type", "")
                if kind == "response.output_text.delta":
                    self._emit_text(event.get("delta", ""), call_id)
                if kind.endswith(".delta") and event.get("delta") and self.activity is not None:
                    self.activity.touch(response=True)
                if kind in {"response.completed", "response.incomplete"}:
                    raw = event["response"]
                    completed = True
                continue
            if event.get("model"):
                raw["model"] = event["model"]
            if event.get("usage") is not None:
                raw["usage"] = event["usage"]
            for choice in event.get("choices", []):
                if choice.get("index", 0) != 0:
                    continue
                if choice.get("finish_reason"):
                    raw["choices"][0]["finish_reason"] = choice["finish_reason"]
                    completed = True
                delta = choice.get("delta", {})
                if any(delta.get(key) for key in ("content", "reasoning_content", "tool_calls", "refusal")) and self.activity is not None:
                    self.activity.touch(response=True)
                if delta.get("content"):
                    message["content"] += delta["content"]
                    self._emit_text(delta["content"], call_id)
                for key in ("reasoning_content", "refusal"):
                    if delta.get(key):
                        message[key] = message.get(key, "") + delta[key]
                for part in delta.get("tool_calls", []):
                    call = calls.setdefault(part["index"], {"id": "", "function": {"name": "", "arguments": ""}})
                    if part.get("id"):
                        call["id"] += part["id"]
                    for key in ("name", "arguments"):
                        call["function"][key] += part.get("function", {}).get(key, "") or ""
                    if part.get("extra_content"):
                        call["extra_content"] = part["extra_content"]
        if not completed:
            raise ConnectionError("Model stream ended before a terminal event")
        if self.protocol == "openai_chat":
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        return raw


def _sse(response: httpx.Response):
    data: list[str] = []
    for line in response.iter_lines():
        if not line:
            if data:
                value = "\n".join(data)
                data = []
                if value == "[DONE]":
                    return
                yield json.loads(value)
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data and "\n".join(data) != "[DONE]":
        yield json.loads("\n".join(data))


def _raise_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # Preserve provider context-length details for the existing recovery classifier.
        try:
            error = response.json().get("error", {})
            detail = error.get("message", "") if isinstance(error, dict) else str(error)
        except (ValueError, AttributeError):
            detail = ""
        raise httpx.HTTPStatusError(f"{exc}: {detail[:2000]}", request=exc.request, response=response) from exc
