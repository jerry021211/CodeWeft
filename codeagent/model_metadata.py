"""Read context limits from the configured provider, with bounded caching.

Only a provider's explicit total-context field is accepted. Output limits and
input-only limits must not be silently presented as a total context window.
"""

from __future__ import annotations

from collections import OrderedDict
from hashlib import sha256
from threading import Lock
import time
from urllib.parse import urlsplit, urlunsplit

import httpx


def models_url(base_url: str, *, protocol: str = "anthropic") -> str:
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Invalid provider URL")
    path = parts.path.rstrip("/")
    # DeepSeek's Messages compatibility prefix is not its model discovery API.
    if parts.hostname == "api.deepseek.com" and path in {"", "/v1", "/anthropic", "/anthropic/v1"}:
        path = "/models"
    elif protocol != "anthropic":
        path += "/models"
    else:
        path = path + ("/models" if path.endswith("/v1") else "/v1/models")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class ModelWindowCache:
    def __init__(self, *, clock=time.monotonic, get=httpx.get) -> None:
        self._clock = clock
        self._get = get
        self._entries: OrderedDict[tuple[str, str, str], tuple[float, dict]] = OrderedDict()
        self._lock = Lock()

    def resolve(self, *, base_url: str, api_key: str, model: str, protocol: str = "anthropic") -> dict:
        def unknown(reason: str) -> dict:
            return {"context_window_tokens": 0, "context_window_source": "unavailable", "context_window_reason": reason}

        if not api_key or not model:
            return unknown("missing_credentials")
        try:
            endpoint = models_url(base_url, protocol=protocol)
        except ValueError:
            return unknown("invalid_endpoint")
        key = (endpoint + "#" + protocol, sha256(api_key.encode()).hexdigest(), model)
        # Coalesce concurrent first requests and share results across run clients.
        with self._lock:
            entry = self._entries.get(key)
            if entry and self._clock() < entry[0]:
                self._entries.move_to_end(key)
                return dict(entry[1])
            headers = {"Authorization": f"Bearer {api_key}"}
            if protocol == "anthropic":
                headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
            try:
                response = self._get(endpoint, headers=headers, timeout=3.0, follow_redirects=False)
                if response.status_code in {401, 403}:
                    result = unknown("unauthorized")
                elif response.status_code in {404, 405}:
                    result = unknown("unsupported_endpoint")
                elif response.status_code != 200:
                    result = unknown("request_failed")
                else:
                    data = response.json()
                    rows = data.get("data") if isinstance(data, dict) else None
                    if not isinstance(rows, list):
                        result = unknown("invalid_response")
                    else:
                        row = next((row for row in rows if isinstance(row, dict) and row.get("id") == model), None)
                        # Official compatibility names, not inferred from substrings.
                        # https://api-docs.deepseek.com/updates/ (2026-09-10)
                        if (row is None and urlsplit(endpoint).hostname == "api.deepseek.com"
                                and model in {"deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}):
                            row = next((row for row in rows if isinstance(row, dict) and row.get("id") == "deepseek-flash"), None)
                        window = row.get("context_window") if row is not None else None
                        if type(window) is int and window > 0:
                            result = {"context_window_tokens": window, "context_window_source": "model_api", "context_window_reason": None,
                                      "context_window_model": row["id"]}
                        else:
                            result = unknown("missing_window" if row is not None else "model_not_found")
                        effort = row.get("effort") if row is not None else None
                        levels = effort.get("supported_levels") if isinstance(effort, dict) else None
                        if (isinstance(levels, list) and levels
                                and all(isinstance(level, str) and level and level not in {"none", "default"}
                                        for level in levels)):
                            result["reasoning_supported_levels"] = tuple(dict.fromkeys(levels))
                            default = effort.get("default_level")
                            result["reasoning_default_level"] = default if default in levels else None
            except httpx.TimeoutException:
                result = unknown("timeout")
            except (httpx.HTTPError, ValueError):
                # Do not persist exception strings containing URLs or credentials.
                result = unknown("request_failed")
            ttl = 3600 if result["context_window_tokens"] else 60
            self._entries[key] = (self._clock() + ttl, result)
            self._entries.move_to_end(key)
            while len(self._entries) > 256:
                self._entries.popitem(last=False)
            return dict(result)


model_windows = ModelWindowCache()
