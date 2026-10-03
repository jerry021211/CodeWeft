"""Explicit opt-in embedding providers. No chat credentials are inherited."""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import math
from threading import RLock
import time
from typing import Protocol

import httpx


class EmbeddingProvider(Protocol):
    provider: str
    model: str
    dimensions: int

    def embed(self, texts: list[str], *, check, timeout: float) -> list[list[float]]:
        """Return ordered vectors; honor the deadline and cooperative check."""
        ...


@dataclass(frozen=True)
class EmbeddingConfig:
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    dimensions: int = 0
    timeout_seconds: float = 10.0
    max_chunks: int = 2000
    batch_size: int = 10

    def create_provider(self):
        if not self.enabled:
            return None
        if not self.base_url or not self.model or self.dimensions < 1:
            raise ValueError("Embedding requires explicit base_url, model and positive dimensions")
        if (not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0 or self.max_chunks < 1
                or type(self.batch_size) is not int or self.batch_size < 1):
            raise ValueError("Embedding timeout, max_chunks and batch_size must be positive")
        return RemoteEmbeddingProvider(self)


class RemoteEmbeddingProvider:
    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self.provider = "openai-compatible:" + config.base_url.rstrip("/")
        self.model = config.model
        self.dimensions = config.dimensions

    def embed(self, texts, *, check, timeout):
        return asyncio.run(self._embed(texts, check, min(timeout, self.config.timeout_seconds)))

    async def _embed(self, texts, check, timeout):
        deadline = time.monotonic() + timeout
        vectors = EmbeddingBatch([], {'input_tokens': 0, 'requests': 0})
        for offset in range(0, len(texts), self.config.batch_size):
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Embedding deadline exceeded')
            batch = await self._embed_batch(texts[offset:offset + self.config.batch_size], check, remaining)
            vectors.extend(batch)
            vectors.usage['requests'] += 1
            count = batch.usage.get('input_tokens')
            vectors.usage['input_tokens'] = (vectors.usage['input_tokens'] + count
                if count is not None and vectors.usage['input_tokens'] is not None else None)
        return vectors

    async def _embed_batch(self, texts, check, timeout):
        check()
        deadline = time.monotonic() + timeout
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key
        async with httpx.AsyncClient(timeout=timeout) as client:
            async def fetch():
                async with client.stream('POST', self.config.base_url.rstrip('/') + '/embeddings',
                        json={'model': self.model, 'input': texts, 'encoding_format': 'float'}, headers=headers) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    async for part in response.aiter_bytes():
                        raw.extend(part)
                        if len(raw) > 16 * 1024 * 1024:
                            raise ValueError('Embedding response exceeds limit')
                    return raw
            task = asyncio.create_task(fetch())
            try:
                while not task.done():
                    check()
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Embedding deadline exceeded')
                    await asyncio.wait({task}, timeout=min(.05, max(0., deadline - time.monotonic())))
                check()
                raw = task.result()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        payload = json.loads(raw)
        data = payload['data']
        if sorted(item["index"] for item in data) != list(range(len(texts))):
            raise ValueError("Embedding response indices do not match request")
        usage = payload.get('usage') or {}
        count = usage.get('prompt_tokens', usage.get('input_tokens'))
        return EmbeddingBatch([item['embedding'] for item in sorted(data, key=lambda item: item['index'])],
                              {'input_tokens': count if type(count) is int and count >= 0 else None, 'requests': 1})


class EmbeddingBatch(list):
    def __init__(self, values, usage):
        super().__init__(values)
        self.usage = usage


class ProviderHandle:
    """Serialize custom/local providers shared by independent Agent services."""
    def __init__(self, provider):
        self.wrapped = provider
        self.lock = RLock()

    @property
    def provider(self):
        return self.wrapped.provider

    @property
    def model(self):
        return self.wrapped.model

    @property
    def dimensions(self):
        return self.wrapped.dimensions

    def embed(self, texts, *, check, timeout):
        deadline = time.monotonic() + timeout
        while not self.lock.acquire(timeout=.02):
            check()
            if time.monotonic() >= deadline:
                raise TimeoutError('Embedding provider busy')
        try:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Embedding deadline exceeded')
            return self.wrapped.embed(texts, check=check, timeout=remaining)
        finally:
            self.lock.release()


def normalized(vector, dimensions):
    if not isinstance(vector, list) or len(vector) != dimensions:
        raise ValueError("Embedding dimension mismatch")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in vector):
        raise ValueError("Non-finite embedding")
    norm = math.hypot(*vector)
    if not norm or not math.isfinite(norm):
        raise ValueError("Invalid embedding norm")
    return [v / norm for v in vector]
