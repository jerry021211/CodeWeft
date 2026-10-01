import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from codeagent.anthropic_client import AnthropicModelClient
from codeagent.model_metadata import ModelWindowCache, models_url
from codeagent.context import ContextManager, ContextConfig
from codeagent.events import CallbackEventSink, EventEmitter


class ModelMetadataTests(unittest.TestCase):
    def test_provider_urls_preserve_gateway_prefix_and_normalize_deepseek(self):
        for base in ("https://api.deepseek.com", "https://api.deepseek.com/anthropic", "https://api.deepseek.com/anthropic/v1/"):
            self.assertEqual(models_url(base), "https://api.deepseek.com/models")
        self.assertEqual(models_url("https://gateway.test/team/v1/"), "https://gateway.test/team/v1/models")
        self.assertEqual(models_url("https://api.anthropic.com"), "https://api.anthropic.com/v1/models")

    def test_cache_expiry_model_and_credentials_isolation(self):
        calls, now = [], [0]
        def get(url, **kwargs):
            calls.append((url, kwargs))
            return httpx.Response(200, json={"data": [{"id": "a", "context_window": 100000}, {"id": "b", "context_window": 200000}]})
        cache = ModelWindowCache(get=get, clock=lambda: now[0])
        def query(model="a", key="secret"):
            return cache.resolve(base_url="https://api.deepseek.com/anthropic", api_key=key, model=model)
        self.assertEqual(query()["context_window_tokens"], 100000)
        query()["context_window_tokens"] = 7
        self.assertEqual(query()["context_window_tokens"], 100000)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0][1]["follow_redirects"])
        self.assertEqual(query("b")["context_window_tokens"], 200000)
        query(key="another")
        self.assertEqual(len(calls), 3)
        now[0] = 3601
        query()
        self.assertEqual(len(calls), 4)

    def test_unavailable_metadata_does_not_invent_a_window(self):
        cases = [
            (httpx.Response(200, json={"data": [{"id": "a", "max_output_tokens": 8000, "max_input_tokens": 100000}]}), "missing_window"),
            (httpx.Response(200, json={"data": [{"id": "other", "context_window": 100000}]}), "model_not_found"),
            (httpx.Response(200, json={"data": [{"id": "a", "context_window": True}]}), "missing_window"),
            (httpx.Response(401), "unauthorized"), (httpx.Response(404), "unsupported_endpoint"),
            (httpx.Response(302), "request_failed"), (httpx.Response(200, json={}), "invalid_response"),
        ]
        for response, reason in cases:
            with self.subTest(reason=reason):
                result = ModelWindowCache(get=lambda *a, **k: response).resolve(base_url="https://provider.test", api_key="key", model="a")
                self.assertEqual(result["context_window_tokens"], 0)
                self.assertEqual(result["context_window_reason"], reason)

    def test_timeout_is_safe_and_retried_after_negative_cache_expires(self):
        now, calls = [0], []
        def get(*args, **kwargs):
            calls.append(1)
            raise httpx.ReadTimeout("do not expose secret")
        cache = ModelWindowCache(get=get, clock=lambda: now[0])
        query = lambda: cache.resolve(base_url="https://provider.test", api_key="key", model="a")
        self.assertEqual(query()["context_window_reason"], "timeout")
        self.assertNotIn("secret", str(query()))
        self.assertEqual(len(calls), 1)
        now[0] = 61
        query()
        self.assertEqual(len(calls), 2)

    def test_fork_queries_actual_shared_sdk_credentials(self):
        sdk = SimpleNamespace(base_url="https://provider.test/v1/", api_key="actual-key")
        client = AnthropicModelClient(sdk_client=sdk).fork()
        with patch("codeagent.anthropic_client.model_windows.resolve", return_value={}) as resolve:
            client.get_model_window("current")
            resolve.assert_called_once_with(base_url=sdk.base_url, api_key=sdk.api_key, model="current")

    def test_official_alias_uses_live_metadata_only_on_official_endpoint(self):
        def get(*args, **kwargs):
            return httpx.Response(200, json={"data": [{"id": "deepseek-flash", "context_window": 1048576}]})
        cache = ModelWindowCache(get=get)
        official = cache.resolve(base_url="https://api.deepseek.com/anthropic", api_key="key", model="deepseek-v4-flash")
        self.assertEqual(official["context_window_tokens"], 1048576)
        self.assertEqual(official["context_window_model"], "deepseek-flash")
        gateway = cache.resolve(base_url="https://gateway.test", api_key="key", model="deepseek-v4-flash")
        self.assertEqual(gateway["context_window_tokens"], 0)

    def test_exact_model_takes_precedence_over_compatibility_alias(self):
        cache = ModelWindowCache(get=lambda *a, **k: httpx.Response(200, json={"data": [
            {"id": "deepseek-flash", "context_window": 2000000},
            {"id": "deepseek-v4-flash", "context_window": 1000000},
        ]}))
        result = cache.resolve(base_url="https://api.deepseek.com", api_key="key", model="deepseek-v4-flash")
        self.assertEqual(result["context_window_tokens"], 1000000)

    def test_discovery_overrides_manual_window_even_on_failure(self):
        manager = ContextManager(config=ContextConfig(context_window_tokens=99999, model_context_windows={"a": 123456}))
        for window in (200000, 0):
            events = []
            info = {"context_window_tokens": window, "context_window_source": "model_api" if window else "unavailable", "context_window_reason": None if window else "missing_window"}
            manager.prepare_before_model_call([{ "role": "user", "content": "hello"}], model="a", model_window=info, event_emitter=EventEmitter(CallbackEventSink(events.append)))
            self.assertEqual(events[-1].payload["context_window_tokens"], window)
            self.assertEqual(events[-1].payload["context_window_source"], info["context_window_source"])
