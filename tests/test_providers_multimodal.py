from __future__ import annotations

import base64
import asyncio
import json
import os
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from codeagent import Agent, AgentConfig, EnvironmentConfig, OpenAIModelClient, ToolRegistry
from codeagent.context.budget import enforce_request
from codeagent.events import CallbackEventSink, EventEmitter, UsageTracker
from codeagent.multimodal import anthropic_messages, user_content, validate_attachments
from codeagent.providers import normalize_response, request_payload
from codeagent.speech import SpeechConfig, prepare_input, validate_input


def attachment(media="image/png", data=b"example", name="test.png"):
    return {"name": name, "media_type": media, "data": base64.b64encode(data).decode()}


def chat(message=None, **extra):
    return {"model": "chat-model", "choices": [{"finish_reason": "stop", "message": message or {"content": "done"}}], **extra}


class ProviderTests(unittest.TestCase):
    def request(self, protocol, messages=None, **kwargs):
        return request_payload(protocol, model="m", system="system", messages=messages or [{"role": "user", "content": "hi"}],
                               tools=[{"name": "echo", "input_schema": {"type": "object"}}], max_tokens=30, **kwargs)

    def test_chat_tool_loop_and_cached_usage(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer chat-key")
            if len(requests) == 1:
                return httpx.Response(200, json=chat({"content": None, "tool_calls": [{"id": "call_1", "function": {"name": "echo", "arguments": '{"x":1}'}}]}))
            self.assertEqual(requests[-1]["messages"][-1], {"role": "tool", "tool_call_id": "call_1", "content": "ok"})
            return httpx.Response(200, json=chat(usage={"prompt_tokens": 10, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 6}}))
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            client = OpenAIModelClient(api_key="chat-key", sdk_client=http)
            first = client.create_message(model="m", system="s", messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=30)
            second = client.create_message(model="m", system="s", messages=[{"role": "assistant", "content": first.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}]}], tools=[], max_tokens=30)
        self.assertEqual(first.stop_reason, "tool_use")
        self.assertEqual(second.usage.total_tokens, 14)
        self.assertEqual(second.usage.cache_read_input_tokens, 6)
        self.assertEqual(second.usage.provider, "openai_chat")

    def test_responses_reasoning_and_tool_roundtrip(self):
        raw = {"status": "completed", "incomplete_details": None, "output": [
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"},
            {"type": "function_call", "call_id": "c1", "name": "echo", "arguments": '{"x":1}'},
        ]}
        response = normalize_response(raw, "openai_responses", "m", "main")
        payload = self.request("openai_responses", [{"role": "assistant", "content": response.content},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}]}])
        self.assertEqual(payload["input"][0], raw["output"][0])
        self.assertEqual(payload["input"][-1], {"type": "function_call_output", "call_id": "c1", "output": "ok"})
        self.assertEqual(payload["max_output_tokens"], 30)
        self.assertFalse(payload["store"])
        self.assertFalse(payload["tools"][0]["strict"])

    def test_chat_stream_interleaved_tools_usage_and_private_reasoning(self):
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "private"}}]},
            {"choices": [{"delta": {"content": "Hello", "tool_calls": [{"index": 1, "id": "b", "function": {"name": "second", "arguments": "{"}}, {"index": 0, "id": "a", "function": {"name": "first", "arguments": "{}"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 1, "function": {"arguments": "}"}, "extra_content": {"google": {"thought_signature": "signature"}}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 2}},
        ]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        emitted, events = [], []
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))) as http:
            client = OpenAIModelClient(sdk_client=http, stream=True, on_text=emitted.append,
                event_emitter=EventEmitter(CallbackEventSink(events.append)), usage_tracker=UsageTracker())
            result = client.create_message(model="m", system="", messages=[], tools=[], max_tokens=10)
            fork = client.fork(call_kind="context_summary", stream=False)
            self.assertIsNot(fork, client)
            self.assertIs(fork.sdk_client, http)
        self.assertEqual(emitted, ["Hello"])
        self.assertEqual([block["id"] for block in result.content if block["type"] == "tool_use"], ["a", "b"])
        self.assertEqual(result.usage.total_tokens, 10)
        replay = self.request("openai_chat", [{"role": "assistant", "content": result.content}])
        self.assertEqual(replay["messages"][-1]["tool_calls"][1]["extra_content"]["google"]["thought_signature"], "signature")

    def test_responses_stream_and_truncation(self):
        raw = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": [{"type": "message", "content": [{"type": "output_text", "text": "Hi"}]}]}
        events = [{"type": "response.output_text.delta", "delta": "Hi"}, {"type": "response.incomplete", "response": raw}]
        body = "".join("event: event\ndata: " + json.dumps(event) + "\n\n" for event in events)
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))) as http:
            client = OpenAIModelClient(protocol="openai_responses", stream=True, sdk_client=http)
            result = client.create_message(model="m", system="", messages=[], tools=[], max_tokens=10)
        self.assertEqual(result.stop_reason, "max_tokens")
        self.assertEqual(result.content, [{"type": "text", "text": "Hi"}])
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text='data: {"choices": []}\n\n'))) as http:
            client = OpenAIModelClient(stream=True, sdk_client=http)
            with self.assertRaises(ConnectionError):
                client.create_message(model="m", system="", messages=[], tools=[])

    def test_http_errors_keep_recovery_status(self):
        from codeagent.recovery.classifier import classify_exception
        from codeagent.recovery.models import RecoveryReason
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429, json={"error": "busy"}))) as http:
            client = OpenAIModelClient(sdk_client=http)
            with self.assertRaises(httpx.HTTPStatusError) as caught:
                client.create_message(model="m", system="", messages=[], tools=[])
            self.assertEqual(classify_exception(caught.exception), RecoveryReason.RATE_LIMIT_RETRY)

    def test_media_mapping_no_mutation(self):
        content = user_content("look", [attachment(), attachment("application/pdf", name="a.pdf")])
        history = [{"role": "user", "content": content}]
        before = deepcopy(history)
        for protocol in ("openai_chat", "openai_responses"):
            payload = self.request(protocol, history)
            serialized = json.dumps(payload)
            self.assertIn("data:image/png;base64", serialized)
            self.assertIn("data:application/pdf;base64", serialized)
        native = anthropic_messages(history)
        self.assertNotIn("title", native[0]["content"][1])
        self.assertEqual(history, before)

    def test_capability_configuration_and_shared_chat_model(self):
        values = {"MODEL_ID": "chat", "MODEL_PROTOCOL": "openai_responses", "OPENAI_API_KEY": "chat-key",
                  "CODEAGENT_SPEECH_ENABLED": "true", "CODEAGENT_SPEECH_MODEL": "asr", "CODEAGENT_SPEECH_BASE_URL": "https://speech.test/v1",
                  "CODEAGENT_SPEECH_API_KEY": "speech-key", "CODEAGENT_EMBEDDING_MODEL": "embed", "CODEAGENT_EMBEDDING_API_KEY": "embed-key"}
        with patch.dict(os.environ, values, clear=True), patch("codeagent.config._load_dotenv"):
            env = EnvironmentConfig.from_env()
        self.assertEqual(env.context_config.summarization_model, "chat")
        self.assertEqual(env.speech_config.api_key, "speech-key")
        self.assertEqual(env.embedding_config.api_key, "embed-key")
        client = env.create_model_client()
        self.assertEqual(client.protocol, "openai_responses")
        self.assertEqual(client.api_key, "chat-key")
        self.assertEqual(client.fork(call_kind="memory_select").protocol, client.protocol)
        with self.assertRaises(ValueError):
            EnvironmentConfig(model_id="x", model_protocol="typo")


class MultimodalTests(unittest.TestCase):
    def test_web_attachment_submission_checkpoint_and_rejection(self):
        from fastapi.testclient import TestClient
        from codeagent.web.api import create_app
        from codeagent.web.scheduler import RunScheduler
        from codeagent.web.storage import SQLiteRepository
        from tests.test_web_scheduler import _FakeFactory
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            repo = SQLiteRepository(workspace / "state.db", recover_incomplete=False)
            factory = _FakeFactory()
            factory.env = SimpleNamespace(model_id="chat", model_protocol="anthropic")
            scheduler = RunScheduler(repo, factory)
            app = create_app(repository=repo, scheduler=scheduler, workspace=workspace, env=factory.env,
                             static_dir=workspace / "missing")
            try:
                with TestClient(app) as client:
                    client.get("/api/health")
                    conversation = repo.create_conversation(workspace=workspace)
                    path = f"/api/conversations/{conversation.id}/runs"
                    self.assertEqual(client.post(path, json={"content": ""}).status_code, 422)
                    self.assertEqual(client.post(path, json={"attachments": [attachment("audio/wav", name="a.wav")]}).status_code, 422)
                    self.assertEqual(client.post(path, json={"attachments": [{**attachment(), "data": "bad!"}]}).status_code, 422)
                    response = client.post(path, json={"attachments": [attachment()]})
                    self.assertEqual(response.status_code, 202, response.text)
                    run_id = response.json()["run_id"]
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and repo.get_run(run_id).status not in {"completed", "failed"}:
                        time.sleep(0.01)
                    run = repo.get_run(run_id)
                    self.assertEqual(run.status, "completed", run.error)
                    checkpoint = repo.get_checkpoint_for_run(run_id)
                    self.assertEqual(checkpoint.messages[0]["content"][1]["type"], "image")
                    message = repo.list_messages(conversation.id)[0]
                    self.assertEqual(message.metadata["attachments"][0]["name"], "test.png")
                    self.assertNotIn("data", message.metadata["attachments"][0])
                    config = client.get("/api/runtime-config").json()
                    self.assertEqual(config["model_services"]["chat"]["protocol"], "anthropic")
            finally:
                scheduler.stop()
                repo.close()

    def test_validation_and_capability_rejection(self):
        for item in [attachment(data=b""), {**attachment(), "data": "not-base64!"}, attachment("application/zip"), attachment("text/plain", b"\xff")]:
            with self.assertRaises(ValueError):
                validate_attachments([item])
        audio = attachment("audio/wav", name="a.wav")
        with self.assertRaises(ValueError):
            validate_input("", [audio], EnvironmentConfig(model_id="m"))
        with self.assertRaises(ValueError):
            validate_input("", [attachment()], EnvironmentConfig(model_id="m", input_modalities=("text",)))

    def test_speech_uses_own_endpoint_and_credentials(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(str(request.url), "https://speech.test/v1/audio/transcriptions")
            self.assertEqual(request.headers["authorization"], "Bearer speech-key")
            self.assertIn(b'asr-model', request.content)
            self.assertIn(b'file', request.content)
            return httpx.Response(200, json={"text": "你好"})
        original = httpx.AsyncClient
        speech = SpeechConfig(enabled=True, model="asr-model", base_url="https://speech.test/v1", api_key="speech-key")
        env = EnvironmentConfig(model_id="chat", api_key="chat-key", speech_config=speech)
        with patch("codeagent.speech.httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(handler), **kw)):
            content = prepare_input("总结", [attachment("audio/wav", name="a.wav"), attachment()], env)
        self.assertIn("你好", content[0]["text"])
        self.assertEqual(content[1]["type"], "image")
        self.assertEqual(len(requests), 1)

    def test_speech_failure_and_cancellation_do_not_reach_chat(self):
        from codeagent.runtime import CancellationToken, CancelledError
        env = EnvironmentConfig(model_id="m", speech_config=SpeechConfig(enabled=True, model="asr", base_url="https://speech.test/v1"))
        audio = attachment("audio/wav", name="a.wav")
        with patch.object(SpeechConfig, "transcribe", side_effect=ValueError("empty response")):
            with self.assertRaises(ValueError):
                prepare_input("", [audio], env)
        token = CancellationToken()
        token.cancel("stop")
        with self.assertRaises(CancelledError):
            prepare_input("", [audio], env, check=token.raise_if_cancelled)

    def test_speech_cancellation_interrupts_in_flight_request(self):
        from codeagent.runtime import CancellationToken, CancelledError
        token = CancellationToken()
        checks, closed = [], []
        async def handler(request):
            try:
                await asyncio.sleep(5)
                return httpx.Response(200, json={"text": "late"})
            finally:
                closed.append(True)
        def check():
            checks.append(True)
            if len(checks) >= 3:
                token.cancel("stop")
            token.raise_if_cancelled()
        original = httpx.AsyncClient
        speech = SpeechConfig(enabled=True, model="asr", base_url="https://speech.test/v1")
        with patch("codeagent.speech.httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(handler), **kw)):
            with self.assertRaises(CancelledError):
                speech.transcribe(attachment("audio/wav", name="a.wav"), check=check)
        self.assertEqual(closed, [True])

    def test_binary_payload_not_counted_as_text_tokens(self):
        content = user_content("look", [attachment(data=b"x" * 1_000_000)])
        original = deepcopy(content)
        budget = enforce_request(model="m", system="", messages=[{"role": "user", "content": content}], tools=[],
                                 max_tokens=1000, max_request_chars=600_000, context_window_tokens=32_000)
        self.assertGreater(budget.request_chars, 1_000_000)
        self.assertLess(budget.estimated_prompt_tokens, 10_000)
        self.assertEqual(content, original)

    def test_media_budget_preserves_json_schema_union_types(self):
        tools = [{"name": "nullable", "input_schema": {"type": "object", "properties": {"value": {"type": ["string", "null"]}}}}]
        budget = enforce_request(model="m", system="", messages=[], tools=tools, max_request_chars=600_000)
        self.assertEqual(budget.media_payload_chars, 0)

    def test_agent_keeps_media_and_text_goal(self):
        from codeagent.models import ModelResponse
        calls = []
        class Client:
            def create_message(self, **kwargs):
                calls.append(deepcopy(kwargs))
                return ModelResponse(stop_reason="end_turn", content=[{"type": "text", "text": "done"}])
        content = user_content("inspect", [attachment()])
        agent = Agent(client=Client(), tools=ToolRegistry(), allow_subagents=False,
                      config=AgentConfig(model="fake", loop_guard=None))
        result = agent.run(content)
        self.assertEqual(result.final_text, "done")
        self.assertTrue(any(block.get("type") == "image" for message in calls[0]["messages"] for block in message["content"] if isinstance(block, dict)))
        self.assertEqual(content[0]["text"], "inspect")


if __name__ == "__main__":
    unittest.main()
