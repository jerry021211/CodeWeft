from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from codeagent import EnvironmentConfig, ModelResponse, PromptMode, create_default_registry
from codeagent.context import ContextConfig
from codeagent.events import EventEmitter, ExecutionContext
from codeagent.memory import MemoryConfig
from codeagent.messages import ToolUse
from codeagent.permissions import WaitingPermissionBroker
from codeagent.permissions.read_only import read_only_tool_guard
from codeagent.runtime import CancellationToken, CancelledError
from codeagent.tools.web_search import WebSearchConfig, WebSearchTool
from codeagent.web.factory import WebAgentFactory, serialize_runtime_state
from codeagent.web.storage import SQLiteRepository


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        self.config = WebSearchConfig(enabled=True, api_key="test-secret")
        self.tool = WebSearchTool(self.config)

    def search(self, payload, **kwargs):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        opener = Mock()
        opener.open.return_value = BytesIO(raw)
        with patch("codeagent.tools.web_search.build_opener", return_value=opener):
            result = self.tool.run(" Python 文档 ", **kwargs)
        return result, opener

    def test_disabled_tool_never_connects_and_is_not_registered(self):
        config = replace(self.config, enabled=False)
        with patch("codeagent.tools.web_search.build_opener") as network:
            result = WebSearchTool(config).run("test")
            registry = create_default_registry(web_search_config=config)
            self.assertNotIn("web_search", registry)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(registry.execute("web_search", {"query": "test"}).status, "error")
            network.assert_not_called()

    def test_missing_key_and_invalid_parameters_never_connect(self):
        with patch("codeagent.tools.web_search.build_opener") as network:
            result = WebSearchTool(replace(self.config, api_key=None)).run("test")
            self.assertIn("TAVILY_API_KEY", result)
            self.assertEqual(result.status, "error")
            for query, limit in [(" ", 5), ("x" * 401, 5), (1, 5), ("test", 0), ("test", 11), ("test", True)]:
                with self.subTest(query=query, limit=limit):
                    self.assertEqual(self.tool.run(query, limit).outcome, "parameter_error")
            network.assert_not_called()

    def test_request_and_bounded_citable_results(self):
        result, opener = self.search({"results": [
            {"title": "Python", "url": "https://docs.python.org/", "content": "文档" * 4000},
            {"title": "unsafe", "url": "javascript:alert(1)", "content": "x"},
            {"title": "credentials", "url": "https://user:pass@example.com/"},
            {"url": "https://example.com/page", "content": "reference"},
        ]})
        self.assertEqual(result.status, "success")
        data = json.loads(result)
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(len(data["results"][0]["content"]), 3000)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.tavily.com/search")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        body = json.loads(request.data)
        self.assertEqual(body["query"], "Python 文档")
        self.assertFalse(body["include_raw_content"])
        self.assertEqual(body["search_depth"], "basic")
        self.assertNotIn("test-secret", repr(self.config))

    def test_provider_result_limit_and_empty_results(self):
        result, _ = self.search({"results": [{"url": "https://example.com/"}] * 20}, max_results=2)
        self.assertEqual(len(json.loads(result)["results"]), 2)
        result, _ = self.search({"results": []})
        self.assertEqual(json.loads(result)["results"], [])
        self.assertEqual(result.status, "success")

    def test_invalid_or_oversized_provider_response(self):
        for payload in [b"not json", b"\xff", b"x" * 1_000_001, [], {"error": "oops"}]:
            with self.subTest(payload_type=type(payload)):
                result, _ = self.search(payload)
                self.assertEqual(result.status, "error")

    def test_network_failures_are_sanitized_without_automatic_retries(self):
        for error in [URLError("test-secret"), TimeoutError("test-secret"),
                      HTTPError("https://api.tavily.com/search", 401, "test-secret", {}, BytesIO(b"test-secret")),
                      HTTPError("https://api.tavily.com/search", 429, "test-secret", {}, BytesIO())]:
            with self.subTest(error=type(error)):
                opener = Mock()
                opener.open.side_effect = error
                with patch("codeagent.tools.web_search.build_opener", return_value=opener):
                    result = self.tool.run("test")
                self.assertEqual(result.status, "error")
                self.assertNotIn("test-secret", result)
                self.assertEqual(opener.open.call_count, 1)

    def test_runtime_budget_and_cancellation(self):
        token = CancellationToken()
        self.tool.bind_runtime(token.raise_if_cancelled, lambda: 2.5)
        _, opener = self.search({"results": []})
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 2.5)
        token.cancel("stopped")
        with patch("codeagent.tools.web_search.build_opener") as network:
            with self.assertRaises(CancelledError):
                self.tool.run("test")
            network.assert_not_called()

    def test_environment_defaults_and_explicit_settings(self):
        with patch("codeagent.config._load_dotenv"), patch.dict("os.environ", {
            "MODEL_ID": "fake", "CONTEXT_COMPACT_MODE": "off",
        }, clear=True):
            self.assertFalse(EnvironmentConfig.from_env().web_search_config.enabled)
        with patch("codeagent.config._load_dotenv"), patch.dict("os.environ", {
            "MODEL_ID": "fake", "CONTEXT_COMPACT_MODE": "off",
            "TAVILY_API_KEY": "test-secret", "CODEAGENT_WEB_SEARCH_ENABLED": "true",
            "CODEAGENT_WEB_SEARCH_TIMEOUT": "8",
        }, clear=True):
            config = EnvironmentConfig.from_env().web_search_config
            self.assertTrue(config.enabled and config.available)
            self.assertEqual(config.timeout_seconds, 8)
        for timeout in [0, -1, float("nan"), float("inf"), 121]:
            with self.assertRaises(ValueError):
                WebSearchConfig(timeout_seconds=timeout)

    def test_enabled_search_is_allowed_in_discuss_and_read_only_children(self):
        registry = create_default_registry(web_search_config=self.config)
        self.assertIn("web_search", registry.read_only_copy())
        self.assertIsNone(read_only_tool_guard(ToolUse(id="search", name="web_search", input={"query": "test"})))

    def test_factory_run_isolation_subagents_and_checkpoint_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SQLiteRepository(root / "state.db", recover_incomplete=False)
            conversation = repo.create_conversation(workspace=str(root))
            env = EnvironmentConfig(
                model_id="fake", data_dir=root / "data", enable_skills=False,
                context_config=ContextConfig(mode="off"), memory_config=MemoryConfig(enabled=False),
                web_search_config=self.config,
            )
            factory = WebAgentFactory(env, root, repo)
            class Client:
                def create_message(self, **kwargs):
                    return ModelResponse(stop_reason="end_turn", content=[{"type": "text", "text": "done"}])
            client = Client()
            kwargs = dict(event_emitter=EventEmitter(context=ExecutionContext(
                conversation_id=conversation.id, run_id="search-test")),
                cancellation=CancellationToken(), permission_broker=WaitingPermissionBroker())
            try:
                with patch.object(EnvironmentConfig, "create_anthropic_client", return_value=client):
                    online = factory.create(**kwargs, web_search_enabled=True, read_only=True)
                    self.assertIn("web_search", online.tools)
                    online.run("test")
                    checkpoint = SimpleNamespace(messages=online.messages, context=serialize_runtime_state(online.context.state))
                    offline = factory.create(**kwargs, web_search_enabled=False, checkpoint=checkpoint)
                    self.assertNotIn("web_search", offline.tools)
                    self.assertIn("web_search", online.tools)
                    self.assertTrue(factory.env.web_search_config.enabled)
                    self.assertNotIn("web_search", offline._subagent_environment()[0])
                    self.assertIn("web_search", online._subagent_environment()[0])
            finally:
                factory.close()
                repo.close()
