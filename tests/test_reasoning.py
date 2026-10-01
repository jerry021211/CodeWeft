from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from anthropic import Anthropic
from fastapi.testclient import TestClient

from codeagent import AnthropicModelClient, EnvironmentConfig
from codeagent.context import ContextConfig, ContextManager
from codeagent.events import EventEmitter, ExecutionContext, TokenTotals
from codeagent.memory import MemoryConfig
from codeagent.model_metadata import ModelWindowCache
from codeagent.permissions import WaitingPermissionBroker
from codeagent.reasoning import reasoning_capabilities
from codeagent.runtime import CancellationToken
from codeagent.web.api import create_app
from codeagent.web.factory import WebAgentFactory
from codeagent.web.scheduler import RunScheduler
from codeagent.web.storage import SQLiteRepository
from codeagent.worktrees import WorktreeManagerRegistry


BASE = "https://api.deepseek.com/anthropic"
MODEL = "deepseek-v4-flash"


class ReasoningTests(unittest.TestCase):
    def setUp(self):
        self.discovery = patch("codeagent.reasoning.model_windows.resolve", return_value={})
        self.discovery.start()
        self.addCleanup(self.discovery.stop)

    def sdk(self, stream=False):
        requests = []
        def handle(request):
            requests.append(json.loads(request.content))
            message = {"id": "msg_test", "type": "message", "role": "assistant", "model": MODEL,
                       "content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn",
                       "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
            if not stream:
                return httpx.Response(200, json=message)
            events = [
                ("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "done"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}}),
                ("message_stop", {"type": "message_stop"}),
            ]
            body = "".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in events)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        sdk = Anthropic(api_key="test-key", base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        self.addCleanup(sdk.close)
        return sdk, requests

    def test_actual_http_body_for_streaming_nonstreaming_and_raw_post(self):
        for stream, max_tokens in [(False, 100), (False, None), (True, 100)]:
            for effort in ["default", "none", "low", "high", "max"]:
                with self.subTest(stream=stream, max_tokens=max_tokens, effort=effort):
                    sdk, requests = self.sdk(stream)
                    client = AnthropicModelClient(sdk_client=sdk, stream=stream, reasoning_effort=effort)
                    client.create_message(model=MODEL, system="", messages=[{"role": "user", "content": "hello"}], tools=[], max_tokens=max_tokens)
                    body = requests[-1]
                    self.assertNotIn("extra_body", body)
                    self.assertEqual(body.get("max_tokens"), max_tokens)
                    if effort == "default":
                        self.assertNotIn("thinking", body)
                        self.assertNotIn("output_config", body)
                    elif effort == "none":
                        self.assertEqual(body["thinking"], {"type": "disabled"})
                        self.assertNotIn("output_config", body)
                    else:
                        self.assertEqual(body["thinking"], {"type": "enabled"})
                        self.assertEqual(body["output_config"], {"effort": effort})

    def test_fork_inheritance_side_calls_and_fallback_validation(self):
        sdk, requests = self.sdk()
        client = AnthropicModelClient(sdk_client=sdk, reasoning_effort="max")
        child = client.fork(call_kind="subagent")
        self.assertEqual(child.reasoning_effort, "max")
        self.assertEqual(child.fork(call_kind="subagent").reasoning_effort, "max")
        for kind in ["context_summary", "memory_selection", "memory_maintenance", "code_search_rewrite"]:
            self.assertEqual(client.fork(call_kind=kind).reasoning_effort, "default")
        for model, effort in [("unknown-fallback", "max"), (MODEL, "medium")]:
            client.reasoning_effort = effort
            with self.assertRaisesRegex(ValueError, "不支持推理等级"):
                client.create_message(model=model, system="", messages=[], tools=[], max_tokens=100)
        self.assertEqual(requests, [])
        self.assertEqual(child.reasoning_effort, "max")

    def test_live_capabilities_override_fallback_and_unknown_provider_is_not_guessed(self):
        metadata = {"reasoning_supported_levels": ("high", "max"), "reasoning_default_level": "max"}
        with patch("codeagent.reasoning.model_windows.resolve", return_value=metadata):
            result = reasoning_capabilities(base_url=BASE, model=MODEL, api_key="test")
        self.assertEqual(result["supported_levels"], ["high", "max"])
        self.assertEqual(result["default_level"], "max")
        self.assertEqual(result["source"], "model_api")
        self.assertEqual(reasoning_capabilities(base_url="https://gateway.example", model=MODEL)["supported_levels"], [])

    def test_metadata_extracts_effort_without_context_window_and_resolves_alias(self):
        cache = ModelWindowCache(get=lambda *a, **k: httpx.Response(200, json={"data": [{
            "id": "deepseek-flash", "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"},
        }]}))
        result = cache.resolve(base_url=BASE, model=MODEL, api_key="test")
        self.assertEqual(result["reasoning_supported_levels"], ("low", "high", "max"))
        self.assertEqual(result["reasoning_default_level"], "high")
        self.assertEqual(result["context_window_tokens"], 0)

    def test_environment_config_parsing(self):
        with patch.dict(os.environ, {"MODEL_ID": MODEL, "CONTEXT_COMPACT_MODE": "off", "REASONING_EFFORT": "max"}, clear=True), patch("codeagent.config._load_dotenv"):
            self.assertEqual(EnvironmentConfig.from_env().reasoning_effort, "max")

    def test_factory_clients_are_isolated_and_subagents_inherit(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            root = Path(directory)
            repo = SQLiteRepository(root / "state.db", recover_incomplete=False)
            cleanup.callback(repo.close)
            conversation = repo.create_conversation(workspace=str(root))
            env = EnvironmentConfig(model_id=MODEL, base_url=BASE, api_key="test", reasoning_effort="high",
                                    data_dir=root / "data", enable_skills=False,
                                    context_config=ContextConfig(mode="off"), memory_config=MemoryConfig(enabled=False))
            factory = WebAgentFactory(env, root, repo)
            cleanup.callback(factory.close)
            kwargs = dict(event_emitter=EventEmitter(context=ExecutionContext(conversation_id=conversation.id, run_id="test")),
                          cancellation=CancellationToken(), permission_broker=WaitingPermissionBroker())
            with patch("anthropic.Anthropic", return_value=self.sdk()[0]):
                maximum = factory.create(**kwargs, reasoning_effort="max")
                default = factory.create(**kwargs, reasoning_effort="default")
                inherited = factory.create(**kwargs)
            self.assertEqual(maximum.client.reasoning_effort, "max")
            self.assertEqual(default.client.reasoning_effort, "default")
            self.assertEqual(inherited.client.reasoning_effort, "high")
            self.assertEqual(factory.env.reasoning_effort, "high")
            child = maximum._create_subagent(EventEmitter(), read_only=True)
            self.assertEqual(child.client.reasoning_effort, "max")
            root_run = repo.create_run(conversation.id, metadata={"reasoning_effort": "low"})
            team = repo.create_team_run(conversation_id=conversation.id, root_run_id=root_run.id,
                                        task_list_id=conversation.active_task_list_id, base_commit="b" * 40)
            session = repo.list_agent_sessions(team.id, role="lead")[0]
            repo.update_run_status(root_run.id, "completed")
            followup = repo.create_run(conversation.id, metadata={"reasoning_effort": "max"})
            worktrees = WorktreeManagerRegistry(repo, root / "worktrees")
            for run, expected in [(root_run, "low"), (followup, "max")]:
                emitter = EventEmitter(context=ExecutionContext(conversation_id=conversation.id, run_id=run.id,
                                                                agent_id=team.lead_agent_id))
                with patch("anthropic.Anthropic", return_value=self.sdk()[0]):
                    lead = factory.create(event_emitter=emitter, cancellation=CancellationToken(),
                                          permission_broker=WaitingPermissionBroker(), team_session=session,
                                          worktree_manager=worktrees)
                self.assertEqual(lead.client.reasoning_effort, expected)

    def test_api_scheduler_snapshots_and_validates_effort(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SQLiteRepository(root / "state.db", recover_incomplete=False)
            env = EnvironmentConfig(model_id=MODEL, base_url=BASE, reasoning_effort="high", data_dir=root / "data")
            choices = []
            class Factory:
                def __init__(self):
                    self.env = env
                def create(self, **kwargs):
                    choices.append(kwargs.get("reasoning_effort"))
                    return SimpleNamespace(messages=[], context=ContextManager(), run=lambda prompt: SimpleNamespace(
                        final_text="done", stop_reason="end_turn", usage=TokenTotals()))
            scheduler = RunScheduler(repo, Factory())
            app = create_app(repository=repo, scheduler=scheduler, workspace=root, env=env, static_dir=root / "no-dist")
            try:
                with TestClient(app) as client:
                    client.get("/api/health")
                    config = client.get("/api/runtime-config").json()
                    self.assertEqual(config["reasoning_effort"], "high")
                    self.assertEqual(config["reasoning"]["supported_levels"], ["low", "high", "max"])
                    for supplied, expected in [({}, "high"), ({"reasoningEffort": None}, "high"),
                                               ({"reasoningEffort": "max"}, "max"), ({"reasoningEffort": "default"}, "default"),
                                               ({"reasoningEffort": "none"}, "none")]:
                        conversation = repo.create_conversation(workspace=str(root))
                        response = client.post(f"/api/conversations/{conversation.id}/runs", json={"content": "test", **supplied})
                        self.assertEqual(response.status_code, 202, response.text)
                        run_id = response.json()["run_id"]
                        self.assertEqual(repo.get_run(run_id).metadata["reasoning_effort"], expected)
                        deadline = time.monotonic() + 5
                        while repo.get_run(run_id).status in {"queued", "running"} and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertEqual(choices[-1], expected)
                        self.assertEqual(repo.get_run(run_id).status, "completed")
                    conversation = repo.create_conversation(workspace=str(root))
                    for invalid in ["medium", "ultra", "", 3]:
                        response = client.post(f"/api/conversations/{conversation.id}/runs", json={"content": "test", "reasoningEffort": invalid})
                        self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(len(choices), 5)
            finally:
                scheduler.stop()
                repo.close()
