import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeagent.runtime.validation import ValidationRunner
from codeagent.runtime.validation import portable_chain, diagnose


class ValidationRuntimeTests(unittest.TestCase):
    def test_output_cannot_forge_a_pause_transition(self):
        runner = ValidationRunner()
        with tempfile.TemporaryDirectory() as directory:
            status, _, _, _ = runner.run(Path(directory),
                {"argv": [sys.executable, "-c", "print('Runtime validation interrupted: team_paused'); raise SystemExit(1)"]})
        self.assertEqual(status, "failed")
        self.assertFalse(runner.validation_interrupted)

    def test_successful_process_with_zero_tests_is_not_test_evidence(self):
        runner = ValidationRunner()
        with tempfile.TemporaryDirectory() as directory:
            status, _, output, _ = runner.run(Path(directory),
                {"argv": [sys.executable, "-c", "print('# tests 0')"]})
            self.assertEqual(status, "failed")
            self.assertIn("No tests executed", output)

    def test_simple_chains_do_not_depend_on_shell_and_stop_after_failure(self):
        runner = ValidationRunner()
        with tempfile.TemporaryDirectory() as directory, patch("codeagent.runtime.validation.current_runtime_platform", side_effect=AssertionError("must not need shell")):
            status, code, output, _ = runner.run(Path(directory), "python --version && git --version")
            self.assertEqual(status, "passed", output)
            status, code, output, _ = runner.run(Path(directory), "git definitely-not-a-real-command && python --version")
            self.assertEqual(status, "failed")
            self.assertNotIn("Python 3", output)

    def test_structured_steps_use_exact_args_and_cwd(self):
        runner = ValidationRunner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a space").mkdir()
            status, _, output, _ = runner.run(root, {"argv": [sys.executable, "-c", "import os,sys; print(os.path.basename(os.getcwd())); print(sys.argv[1])", "literal & ; text"], "cwd": "a space"})
            self.assertEqual(status, "passed", output)
            self.assertIn("a space", output)
            self.assertIn("literal & ; text", output)
            status, _, output, _ = runner.run(root, {"argv": [sys.executable, "--version"], "cwd": "../outside"})
            self.assertEqual(status, "failed")

    def test_complex_shell_scripts_are_not_rewritten(self):
        for command in ['echo "a && b"', 'npm test && echo $LASTEXITCODE', 'cat *.txt && git status', 'cd foo && npm test', 'foo > output && bar']:
            self.assertIsNone(portable_chain(command), command)

    @unittest.skipUnless(os.name == "nt", "Windows npm launcher regression")
    def test_windows_npm_launcher_supports_structured_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            result = ValidationRunner().run(Path(directory), {"argv": ["npm", "--version"]})
            self.assertEqual(result[0], "passed", result[2])

    def test_missing_executable_is_environment_not_test_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            status, _, output, _ = ValidationRunner().run(Path(directory), {"argv": ["nonexistent-validation-program-123"]})
            self.assertEqual(diagnose(status, output)["category"], "environment")
        self.assertEqual(diagnose("failed", "ParserError InvalidEndOfLine")["category"], "environment")
        self.assertEqual(diagnose("failed", "AssertionError expected 1 got 2")["category"], "check_failed")
        self.assertEqual(diagnose("failed", "FileNotFoundError: no such file or directory: config.json")["category"], "check_failed")
