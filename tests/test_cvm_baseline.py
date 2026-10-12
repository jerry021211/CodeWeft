"""Stage 0 compatibility assertions apply ONLY to CVM off, not future modes."""
from dataclasses import FrozenInstanceError, fields
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from codeagent import AgentConfig, CvmConfig, EnvironmentConfig
from codeagent.planning import PlanningBackend
from cvm_fixtures import GOLDEN, ScriptedClient, assess, digest, pack_traces, run_case, scenarios
from run_cvm_baseline import run_checked


class CvmConfigTests(unittest.TestCase):
    def setUp(self):
        # Resolve temp storage before clearing environment variables. Otherwise
        # Windows tempfile may cache the repository as its fallback directory.
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = directory.name

    def test_default_is_off_and_snapshot_is_detached(self):
        config = CvmConfig()
        self.assertEqual(config.snapshot(), {"schema_version": 1, **{f.name: "off" for f in fields(config)}})
        snapshot = config.snapshot()
        snapshot["completion"] = "active"
        self.assertEqual(config.completion, "off")
        with self.assertRaises(FrozenInstanceError):
            config.contract = "active"
        self.assertEqual(AgentConfig(model="offline").cvm, config)

    def test_unimplemented_modes_and_invalid_values_fail_closed(self):
        for item in fields(CvmConfig):
            values = ("invalid", "", None, True) if item.name == "contract" else ("shadow", "active", "invalid", "", None, True)
            for value in values:
                with self.subTest(component=item.name, value=value), self.assertRaises(ValueError):
                    CvmConfig(**{item.name: value})

    def test_environment_flows_to_shared_cli_web_agent_config(self):
        settings = {"MODEL_ID": "offline", "CONTEXT_COMPACT_MODE": "off", "CODEAGENT_DATA_DIR": self.data_dir}
        settings.update({f"CODEAGENT_CVM_{item.name.upper()}": " OFF " for item in fields(CvmConfig)})
        with patch.dict(os.environ, settings, clear=True), patch("codeagent.config._load_dotenv"), \
             patch("codeagent.model_settings.read_settings", return_value=None):
            env = EnvironmentConfig.from_env()
        self.assertEqual(env.cvm_config, CvmConfig())
        self.assertIs(env.to_agent_config().cvm, env.cvm_config)
        self.assertIs(env.to_agent_config(planning_backend=PlanningBackend.TASKS).cvm, env.cvm_config)

    def test_environment_rejects_nonoff_components(self):
        for item in fields(CvmConfig):
            if item.name == "contract":
                continue  # Stage 1 implements contract modes; other components remain off.
            with self.subTest(component=item.name), patch.dict(os.environ, {
                "MODEL_ID": "offline", f"CODEAGENT_CVM_{item.name.upper()}": "active",
                "CODEAGENT_DATA_DIR": self.data_dir,
            }, clear=True), patch("codeagent.config._load_dotenv"), \
                 patch("codeagent.model_settings.read_settings", return_value=None):
                with self.assertRaisesRegex(ValueError, "supports off"):
                    EnvironmentConfig.from_env()


class CvmEntryPointConfigTests(unittest.TestCase):
    def make_env(self, root):
        from codeagent.context import ContextConfig
        from codeagent.lsp.registry import LspConfig
        from codeagent.memory import MemoryConfig
        return EnvironmentConfig(model_id="offline", data_dir=root / "data", enable_skills=False,
                                 memory_config=MemoryConfig(enabled=False), lsp_config=LspConfig(enabled=False),
                                 context_config=ContextConfig(mode="off"), cvm_config=CvmConfig())

    def test_cli_constructs_agent_with_shared_cvm_config(self):
        from codeagent.cli import main
        with TemporaryDirectory() as directory:
            env = self.make_env(Path(directory))
            with patch("codeagent.cli.EnvironmentConfig.from_env", return_value=env), \
                 patch.object(EnvironmentConfig, "create_model_client"), \
                 patch("codeagent.cli.Agent") as agent_type, patch("codeagent.cli.McpRouter"), \
                 redirect_stdout(io.StringIO()):
                agent_type.return_value.run.return_value = SimpleNamespace(stop_reason="end_turn", final_text="done")
                self.assertEqual(main(["inspect"]), 0)
            self.assertIs(agent_type.call_args.kwargs["config"].cvm, env.cvm_config)

    def test_real_web_factory_and_child_inherit_same_immutable_config(self):
        from codeagent.events import EventEmitter, ExecutionContext
        from codeagent.permissions import WaitingPermissionBroker
        from codeagent.runtime import CancellationToken
        from codeagent.web.factory import WebAgentFactory
        from codeagent.web.storage import SQLiteRepository
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env = self.make_env(root)
            repository = SQLiteRepository(root / "state.db", recover_incomplete=False)
            factory = WebAgentFactory(env, root, repository)
            try:
                conversation = repository.create_conversation(title="CVM off", workspace=str(root))
                client = ScriptedClient([])
                with patch.object(EnvironmentConfig, "create_model_client", return_value=client):
                    agent = factory.create(event_emitter=EventEmitter(context=ExecutionContext(
                        conversation_id=conversation.id, run_id="cvm-config-test")),
                        cancellation=CancellationToken(), permission_broker=WaitingPermissionBroker())
                self.assertIs(agent.config.cvm, env.cvm_config)
                with patch.object(client, "fork", return_value=ScriptedClient([]), create=True):
                    child = agent._create_subagent(EventEmitter())
                self.assertIs(child.config.cvm, env.cvm_config)
                self.assertEqual(child.config.cvm.snapshot(), env.cvm_config.snapshot())
            finally:
                factory.close()
                repository.close()


class CvmOffBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.legacy = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def test_inputs_are_frozen_and_cover_six_distinct_categories(self):
        self.assertEqual(scenarios(), self.legacy["scenarios"])
        self.assertEqual(len(scenarios()), 6)
        self.assertEqual(len({case["classification"] for case in scenarios()}), 6)
        self.assertTrue(all(case["desired"] for case in scenarios()))

    def test_legacy_contains_valid_full_payload_hashes_and_source_identity(self):
        self.assertEqual(self.legacy["kind"], "legacy-before-cvm-config")
        self.assertRegex(self.legacy["manifest"]["commit"], r"^[a-f0-9]{40}$")
        self.assertIn("codeagent/agent.py", self.legacy["manifest"]["source_sha256"])
        self.assertFalse(any(line[3:].startswith("codeagent/") for line in self.legacy["manifest"]["dirty_files"]))
        for key, value in self.legacy["request_blobs"].items():
            self.assertEqual(key, digest(value))
        for trace in self.legacy["traces"]:
            for request in trace["requests"]:
                for key in request.values():
                    self.assertIn(key, self.legacy["request_blobs"])

    def assert_off_case(self, identifier):
        case = next(case for case in scenarios() if case["id"] == identifier)
        expected = next(trace for trace in self.legacy["traces"] if trace["id"] == identifier)
        # Defaults and explicit off both match an independently captured pre-change artifact.
        for config in (None, CvmConfig()):
            with self.subTest(explicit_off=config is not None):
                trace = run_case(case, cvm=config)
                packed = pack_traces([trace])
                self.assertEqual(packed["traces"][0], expected)
                for key, value in packed["request_blobs"].items():
                    self.assertEqual(value, self.legacy["request_blobs"][key])
                observation = assess(case, trace)
                self.assertTrue(observation["baseline_observed"])
                self.assertEqual(observation["known_gap_observed"], identifier != "normal_investigation")

    def test_off_followup_goal_overwrite_is_recorded(self):
        self.assert_off_case("followup_goal")

    def test_off_failed_test_claim_is_recorded(self):
        self.assert_off_case("failed_test_claim")

    def test_off_edit_after_pass_is_recorded(self):
        self.assert_off_case("edit_after_pass")

    def test_off_unrelated_pass_is_recorded(self):
        self.assert_off_case("unrelated_pass")

    def test_off_missing_environment_is_recorded(self):
        self.assert_off_case("missing_environment")

    def test_off_normal_investigation_remains_unblocked(self):
        self.assert_off_case("normal_investigation")


class CvmTestEntryPointTests(unittest.TestCase):
    def run_fixture(self, body, pattern="test_sample.py"):
        with TemporaryDirectory() as directory:
            Path(directory, "test_sample.py").write_text("import unittest\n" + body, encoding="utf-8")
            # Discovery caches imported module names; isolate each synthetic module.
            with patch.dict("sys.modules"):
                import sys
                sys.modules.pop("test_sample", None)
                return run_checked([pattern], start_dir=Path(directory), stream=io.StringIO())

    def test_zero_collection_fails(self):
        with self.assertRaisesRegex(ValueError, "Zero tests collected"):
            self.run_fixture("", pattern="test_missing_*.py")

    def test_all_skipped_fails(self):
        with self.assertRaisesRegex(ValueError, "entirely skipped"):
            self.run_fixture("class Sample(unittest.TestCase):\n"
                             " @unittest.skip('fixture')\n def test_one(self): pass\n")

    def test_failed_assertions_are_not_success(self):
        result = self.run_fixture("class Sample(unittest.TestCase):\n def test_one(self): self.fail('fixture')\n")
        self.assertFalse(result.wasSuccessful())

    def test_skipped_pattern_cannot_hide_behind_passing_pattern(self):
        with TemporaryDirectory() as directory, patch.dict("sys.modules"):
            root = Path(directory)
            root.joinpath("cvm_runner_pass.py").write_text(
                "import unittest\nclass Sample(unittest.TestCase):\n def test_one(self): pass\n", encoding="utf-8")
            root.joinpath("cvm_runner_skip.py").write_text(
                "import unittest\n@unittest.skip('fixture')\nclass Sample(unittest.TestCase):\n def test_one(self): pass\n",
                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Pattern entirely skipped"):
                run_checked(["cvm_runner_pass.py", "cvm_runner_skip.py"], start_dir=root, stream=io.StringIO())

    def test_nonempty_passing_suite_succeeds(self):
        result = self.run_fixture("class Sample(unittest.TestCase):\n def test_one(self): self.assertEqual(1, 1)\n")
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(result.testsRun, 1)


if __name__ == "__main__":
    unittest.main()
