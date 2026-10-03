"""Model settings must survive restarts without leaking or mixing credentials."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from codeagent.config import EnvironmentConfig, embedding_config_from_env
from codeagent.model_settings import read_settings, settings_path
from codeagent.web.api import create_app
from codeagent.web.scheduler import RunScheduler
from codeagent.web.storage import SQLiteRepository


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = EnvironmentConfig(model_id="old-chat", api_key="private-chat-key", data_dir=self.root)
        self.repo = SQLiteRepository(self.root / "test.db", recover_incomplete=False)
        self.addCleanup(self.repo.close)
        self.factory = SimpleNamespace(env=self.env)
        self.scheduler = RunScheduler(self.repo, self.factory)
        self.app = create_app(repository=self.repo, scheduler=self.scheduler, env=self.env, workspace=self.root)
        self.client = self.enterContext(TestClient(self.app))
        self.client.get("/api/health")

    def draft(self):
        body = self.client.get("/api/settings/models").json()
        body.pop("configured")
        for service in body["services"].values():
            service.pop("has_api_key")
            service["api_key"] = None
        return body

    def save(self, body):
        return self.client.put("/api/settings/models", json=body)

    def test_save_apply_redaction_restart_and_shared_chat(self):
        initial = self.client.get("/api/settings/models")
        self.assertNotIn("private-chat-key", initial.text)
        self.assertTrue(initial.json()["services"]["chat"]["has_api_key"])
        body = self.draft()
        body["services"]["chat"].update(model="new-chat", protocol="openai_responses", base_url="http://localhost:1234/v1", api_key="new-chat-key")
        body["services"]["speech"].update(enabled=True, model="speech-model", base_url="http://localhost:2345/v1", api_key="speech-key")
        body["services"]["embedding"].update(enabled=True, model="vector-model", base_url="http://localhost:3456/v1", api_key="vector-key", dimensions=768)
        response = self.save(body)
        self.assertEqual(response.status_code, 200, response.text)
        for key in ("new-chat-key", "speech-key", "vector-key"):
            self.assertNotIn(key, response.text)
        self.assertIs(self.factory.env, self.app.state.environment)
        self.assertEqual(self.factory.env.model_id, "new-chat")
        self.assertEqual(self.factory.env.context_config.summarization_model, "new-chat")
        self.assertEqual(self.client.get("/api/runtime-config").json()["model"], "new-chat")
        # Saved values take precedence even over invalid obsolete provider variables.
        variables = {"CODEAGENT_DATA_DIR": str(self.root), "MODEL_PROTOCOL": "obsolete", "MAX_TOKENS": "invalid", "CODEAGENT_SPEECH_ENABLED": "true"}
        with patch.dict(os.environ, variables, clear=True), patch("codeagent.config._load_dotenv"):
            restored = EnvironmentConfig.from_env()
            self.assertEqual(restored.model_id, "new-chat")
            self.assertEqual(restored.speech_config.api_key, "speech-key")
            self.assertEqual(embedding_config_from_env().model, "vector-model")
        self.assertEqual(read_settings(self.root)["revision"], response.json()["revision"])

    def test_preserve_clear_and_changed_endpoint_credentials(self):
        body = self.draft()
        body["services"]["chat"]["max_tokens"] = 512
        self.assertEqual(self.save(body).status_code, 200)
        self.assertEqual(self.factory.env.api_key, "private-chat-key")
        body = self.draft()
        body["services"]["chat"]["base_url"] = "https://different.example/v1"
        self.assertEqual(self.save(body).status_code, 422)
        self.assertEqual(self.factory.env.api_key, "private-chat-key")
        body["services"]["chat"]["api_key"] = ""
        self.assertEqual(self.save(body).status_code, 200)
        self.assertIsNone(self.factory.env.api_key)
        self.assertFalse(self.client.get("/api/settings/models").json()["services"]["chat"]["has_api_key"])

    def test_stale_save_is_rejected_without_overwriting(self):
        first = self.draft()
        stale = copy.deepcopy(first)
        first["services"]["chat"]["model"] = "first-model"
        self.assertEqual(self.save(first).status_code, 200)
        self.assertEqual(self.save(stale).status_code, 409)
        self.assertEqual(read_settings(self.root)["services"]["chat"]["model"], "first-model")

    def test_failed_disk_write_keeps_current_environment(self):
        body = self.draft()
        body["services"]["chat"]["model"] = "unsaved"
        with patch("codeagent.model_settings.os.replace", side_effect=OSError("disk failure")):
            response = self.save(body)
        self.assertEqual(response.status_code, 503)
        self.assertIs(self.factory.env, self.env)
        self.assertFalse(settings_path(self.root).exists())
        self.assertEqual(list((self.root / "settings").glob("*.tmp")), [])

    def test_queued_and_running_tasks_prevent_save(self):
        conversation = self.repo.create_conversation(workspace=self.root)
        run = self.repo.create_run(conversation.id)
        for status in ("queued", "running"):
            if status == "running":
                self.repo.start_run(run.id)
            self.assertEqual(self.save(self.draft()).status_code, 409)
        self.assertFalse(settings_path(self.root).exists())
        self.repo.update_run_status(run.id, "completed")
        self.assertEqual(self.save(self.draft()).status_code, 200)

    def test_unfinished_team_prevents_save(self):
        with patch.object(self.repo, "list_team_runs", return_value=[SimpleNamespace(state=SimpleNamespace(value="waiting_approval"))]):
            self.assertEqual(self.save(self.draft()).status_code, 409)

    def test_invalid_fields_do_not_apply(self):
        for fields in ({"model": " "}, {"base_url": "file:///tmp"}, {"base_url": "https://user:pass@example.com"}, {"max_tokens": 0}, {"input_modalities": []}, {"base_url": "http://localhost:no-port"}):
            with self.subTest(fields=fields):
                body = self.draft()
                body["services"]["chat"].update(fields, api_key="")
                self.assertEqual(self.save(body).status_code, 422)
        body = self.draft()
        body["services"]["embedding"].update(enabled=True, model="vector", base_url="http://localhost/v1", dimensions=0)
        self.assertEqual(self.save(body).status_code, 422)
        self.assertIs(self.factory.env, self.env)

    def test_discovery_uses_only_selected_service_and_never_runs_inference(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"data": [{"id": "speech-b"}, {"id": "speech-a"}, {"id": "speech-a"}]})
        body = self.draft()
        # A pending unrelated chat endpoint edit must not block speech discovery.
        body["services"]["chat"]["base_url"] = "http://different.example/v1"
        body["services"]["speech"].update(base_url="http://speech.example/v1", api_key="speech-secret")
        upstream = httpx.Client(transport=httpx.MockTransport(respond))
        with patch("codeagent.model_settings.httpx.Client", return_value=upstream):
            response = self.client.post("/api/settings/models/discover", json={**body, "service": "speech"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["models"], ["speech-a", "speech-b"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), "http://speech.example/v1/models")
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(requests[0].headers["authorization"], "Bearer speech-secret")
        self.assertFalse(settings_path(self.root).exists())

    def test_discovery_does_not_echo_provider_error_or_credentials(self):
        body = self.draft()
        upstream = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401, text="private-chat-key")))
        with patch("codeagent.model_settings.httpx.Client", return_value=upstream):
            response = self.client.post("/api/settings/models/discover", json={**body, "service": "chat"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("private-chat-key", response.text)
        self.assertIn("401", response.text)

    def test_settings_mutations_require_local_session(self):
        body = self.draft()
        self.assertEqual(self.client.put("/api/settings/models", json=body, headers={"Origin": "https://evil.example"}).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.save(body).status_code, 403)

    def probe(self, body, reply, *, status=200):
        requests = []
        def respond(request):
            requests.append(request)
            if isinstance(reply, Exception):
                raise reply
            return httpx.Response(status, json=reply)
        upstream = httpx.Client(transport=httpx.MockTransport(respond))
        with patch("codeagent.model_probe.httpx.Client", return_value=upstream):
            response = self.client.post("/api/settings/models/test", json=body)
        return response, requests

    def test_chat_probe_calls_selected_model_for_each_protocol_without_saving(self):
        for protocol, path, reply in (
            ("anthropic", "/v1/messages", {"type": "message", "content": [{"type": "text", "text": "OK"}]}),
            ("openai_chat", "/v1/chat/completions", {"choices": [{"message": {"content": "OK"}}]}),
            ("openai_responses", "/v1/responses", {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]}),
        ):
            with self.subTest(protocol=protocol):
                body = self.draft()
                body["service"] = "chat"
                body["services"]["chat"].update(protocol=protocol, model="unsaved-model", base_url="http://local.test/v1", api_key="probe-key")
                response, requests = self.probe(body, reply)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["ok"], response.text)
                self.assertGreaterEqual(response.json()["elapsed_ms"], 0)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0].method, "POST")
                self.assertEqual(requests[0].url.path, path)
                payload = json.loads(requests[0].content)
                self.assertEqual(payload["model"], "unsaved-model")
                self.assertFalse(payload["stream"])
                self.assertEqual(requests[0].headers["x-api-key" if protocol == "anthropic" else "authorization"], "probe-key" if protocol == "anthropic" else "Bearer probe-key")
                if protocol == "openai_responses":
                    self.assertFalse(payload["store"])
                self.assertNotIn("probe-key", response.text)
                self.assertFalse(settings_path(self.root).exists())
                self.assertIs(self.factory.env, self.env)

    def test_embedding_probe_validates_dimensions_and_does_not_use_chat_credentials(self):
        body = self.draft()
        body["service"] = "embedding"
        body["services"]["embedding"].update(model="vector", base_url="http://vectors.test/v1", api_key="vector-key", dimensions=3)
        for vector, ok in (([0.1, 0.2, 0.3], True), ([0.1, 0.2], False), (["bad", 0.2, 0.3], False)):
            response, requests = self.probe(body, {"data": [{"embedding": vector}]})
            self.assertEqual(response.json()["ok"], ok, response.text)
            self.assertEqual(requests[0].url.path, "/v1/embeddings")
            self.assertEqual(requests[0].headers["authorization"], "Bearer vector-key")
            self.assertEqual(json.loads(requests[0].content)["model"], "vector")

    def test_speech_probe_sends_generated_wav_and_accepts_silence_response(self):
        body = self.draft()
        body["service"] = "speech"
        body["services"]["speech"].update(model="transcribe", base_url="http://speech.test/v1", api_key="speech-key")
        response, requests = self.probe(body, {"text": ""})
        self.assertTrue(response.json()["ok"], response.text)
        self.assertEqual(requests[0].url.path, "/v1/audio/transcriptions")
        self.assertIn("multipart/form-data", requests[0].headers["content-type"])
        self.assertIn(b"connection-test.wav", requests[0].content)
        self.assertIn(b"RIFF", requests[0].content)
        self.assertIn(b"transcribe", requests[0].content)
        self.assertNotIn(b"private-chat-key", requests[0].content)

    def test_probe_reports_http_failure_timeout_and_invalid_protocol_response(self):
        body = {**self.draft(), "service": "chat"}
        for status in (401, 403, 404, 429, 500):
            response, requests = self.probe(body, {"error": {"message": "private-chat-key"}}, status=status)
            self.assertFalse(response.json()["ok"])
            self.assertEqual(response.json()["status_code"], status)
            self.assertNotIn("private-chat-key", response.text)
            self.assertEqual(len(requests), 1)
        for reply in ({"data": [{"id": "just-a-list"}]}, {"type": "message", "content": []}, httpx.ReadTimeout("private-chat-key")):
            response, _ = self.probe(body, reply)
            self.assertFalse(response.json()["ok"], response.text)
            self.assertNotIn("private-chat-key", response.text)

    def test_probe_rejects_missing_model_stale_revision_and_moved_credentials_before_network(self):
        for changed in ("model", "revision", "endpoint"):
            body = {**self.draft(), "service": "chat"}
            if changed == "model":
                body["services"]["chat"]["model"] = " "
            elif changed == "revision":
                body["revision"] = "stale"
            else:
                body["services"]["chat"]["base_url"] = "http://different.test/v1"
            response, requests = self.probe(body, {})
            self.assertEqual(response.status_code, 409 if changed == "revision" else 422)
            self.assertEqual(requests, [])

    def test_first_launch_without_env_can_open_and_configure(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"CODEAGENT_DATA_DIR": folder}, clear=True), patch("codeagent.config._load_dotenv"):
            app = create_app(workspace=folder)
            with TestClient(app) as client:
                response = client.get("/api/settings/models")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertFalse(body.pop("configured"))
                for service in body["services"].values():
                    service.pop("has_api_key")
                    service["api_key"] = ""
                body["services"]["chat"]["model"] = "configured-in-browser"
                self.assertEqual(client.put("/api/settings/models", json=body).status_code, 200)
            with TestClient(create_app(workspace=folder)) as restarted:
                self.assertEqual(restarted.get("/api/settings/models").json()["services"]["chat"]["model"], "configured-in-browser")


if __name__ == "__main__":
    unittest.main()
