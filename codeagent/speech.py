"""Independent, opt-in speech recognition through /audio/transcriptions."""
from __future__ import annotations

import asyncio
import base64
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

from codeagent.multimodal import AUDIO_TYPES, user_content, validate_attachments, validate_modalities


@dataclass(frozen=True, slots=True)
class SpeechConfig:
    enabled: bool = False
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    language: str | None = None
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if self.enabled and (not self.model or not self.base_url):
            raise ValueError("语音识别需要独立配置 CODEAGENT_SPEECH_MODEL 和 CODEAGENT_SPEECH_BASE_URL")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("CODEAGENT_SPEECH_TIMEOUT must be positive")

    def transcribe(self, attachment: dict, *, check=lambda: None) -> str:
        if not self.enabled:
            raise ValueError("Speech recognition is disabled")
        return asyncio.run(self._transcribe(attachment, check))

    async def _transcribe(self, attachment: dict, check) -> str:
        check()
        deadline = time.monotonic() + self.timeout_seconds
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = {"model": self.model, "response_format": "json"}
        if self.language:
            data["language"] = self.language
        files = {"file": (attachment["name"], base64.b64decode(attachment["data"]), attachment["media_type"])}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            task = asyncio.create_task(client.post(self.base_url.rstrip("/") + "/audio/transcriptions",
                                                   headers=headers, data=data, files=files))
            try:
                while not task.done():
                    check()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Speech transcription deadline exceeded")
                    await asyncio.wait({task}, timeout=0.05)
                check()
                response = task.result()
                response.raise_for_status()
                text = response.json().get("text")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("语音识别未返回有效文字")
                return text
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def validate_input(text: str, attachments: list[dict], env: Any) -> None:
    attachments = validate_attachments(attachments)
    speech = getattr(env, "speech_config", SpeechConfig())
    native = [item for item in attachments if not (speech.enabled and item["media_type"] in AUDIO_TYPES)]
    validate_modalities([{"content": user_content(text, native)}], getattr(env, "model_protocol", "anthropic"),
                        getattr(env, "input_modalities", None))


def prepare_input(text: str, attachments: list[dict], env: Any, *, check=lambda: None, emitter=None):
    validate_input(text, attachments, env)
    speech = getattr(env, "speech_config", SpeechConfig())
    native, transcripts = [], []
    for item in attachments:
        check()
        if speech.enabled and item["media_type"] in AUDIO_TYPES:
            info = {"model": speech.model, "name": item["name"], "call_kind": "speech_transcription"}
            if emitter:
                emitter.emit("speech.started", info)
            try:
                transcript = speech.transcribe(item, check=check)
            except Exception as exc:
                if emitter:
                    emitter.emit("speech.failed", {**info, "error_type": type(exc).__name__})
                raise
            transcripts.append(f"音频 {item['name']} 的转写：\n{transcript}")
            if emitter:
                emitter.emit("speech.completed", {**info, "characters": len(transcript)})
        else:
            native.append(item)
    check()
    return user_content("\n\n".join([text, *transcripts]).strip(), native)
