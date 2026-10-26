from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codeagent.web import cli


class WebCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.run = Mock()
        self.uvicorn = patch.dict("sys.modules", {"uvicorn": SimpleNamespace(run=self.run)})
        self.uvicorn.start()
        self.addCleanup(self.uvicorn.stop)

    def test_normal_start_keeps_existing_in_process_behavior(self):
        with patch("codeagent.web.api.create_app") as build:
            self.assertEqual(cli.main(["--workspace", str(self.workspace), "--port", "9001"]), 0)
        build.assert_called_once_with(workspace=self.workspace)
        self.run.assert_called_once_with(build.return_value, host="127.0.0.1", port=9001)

    def test_reload_builds_only_in_child_and_preserves_workspace(self):
        with patch("codeagent.web.api.create_app") as build, patch.dict(os.environ):
            os.environ.pop(cli._RELOAD_WORKSPACE_ENV, None)
            def run(app, **kwargs):
                self.assertEqual(app, "codeagent.web.cli:create_reload_app")
                build.assert_not_called()
                self.assertTrue(kwargs["reload"])
                self.assertTrue(kwargs["factory"])
                self.assertEqual(kwargs["host"], "localhost")
                self.assertEqual(kwargs["port"], 9002)
                self.assertEqual(kwargs["reload_dirs"], [str(Path(cli.__file__).resolve().parents[1])])
                self.assertNotIn(str(self.workspace), kwargs["reload_dirs"])
                self.assertEqual(kwargs["timeout_graceful_shutdown"], 5)
                self.assertIs(cli.create_reload_app(), build.return_value)
                build.assert_called_once_with(workspace=self.workspace)
            self.run.side_effect = run
            self.assertEqual(cli.main(["--reload", "--workspace", str(self.workspace),
                                       "--host", "localhost", "--port", "9002"]), 0)
            self.assertNotIn(cli._RELOAD_WORKSPACE_ENV, os.environ)

    def test_reload_restores_environment_even_when_server_fails(self):
        self.run.side_effect = RuntimeError("bind failed")
        with patch.dict(os.environ, {cli._RELOAD_WORKSPACE_ENV: "previous value"}):
            with self.assertRaisesRegex(RuntimeError, "bind failed"):
                cli.main(["--reload", "--workspace", str(self.workspace)])
            self.assertEqual(os.environ[cli._RELOAD_WORKSPACE_ENV], "previous value")

    def test_invalid_workspace_and_remote_host_fail_before_server_start(self):
        for arguments in [
            ["--reload", "--workspace", str(self.workspace / "missing")],
            ["--reload", "--host", "0.0.0.0"],
        ]:
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    cli.main(arguments)
                self.assertEqual(exc.exception.code, 2)
        self.run.assert_not_called()

    def test_help_explains_reload_and_active_task_boundary(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exc:
            cli.main(["--help"])
        self.assertEqual(exc.exception.code, 0)
        self.assertIn("--reload", output.getvalue())
        self.assertIn("interrupts active tasks", output.getvalue())


if __name__ == "__main__":
    unittest.main()
