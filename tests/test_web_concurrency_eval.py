from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evals.web_concurrency import ScriptedFactory, run_case


class WebConcurrencyEvalTests(unittest.IsolatedAsyncioTestCase):
    async def case(self, **overrides):
        options = dict(requests=4, delay=0.05, events=5, timeout=10)
        options.update(overrides)
        with tempfile.TemporaryDirectory() as directory:
            return await run_case(Path(directory) / "case", 2, 4, SimpleNamespace(**options))

    async def test_real_scheduler_persists_isolated_results(self):
        result = await self.case()
        self.assertEqual(result["successes"], 4)
        self.assertEqual(result["failures"], 0)
        self.assertTrue(result["capacity_respected"])
        self.assertEqual(len({row["run_id"] for row in result["samples"]}), 4)
        self.assertEqual(result["completion"]["count"], 4)

    async def test_sse_replay_includes_events_beyond_one_page(self):
        result = await self.case(requests=1, events=505, delay=0)
        self.assertEqual(result["successes"], 1)
        self.assertGreater(result["samples"][0]["event_count"], 505)

    async def test_timeout_is_a_failure_not_a_success_latency_sample(self):
        result = await self.case(requests=1, delay=1, events=1, timeout=0.05)
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["completion"]["count"], 0)
        self.assertIsNone(result["completion"]["p95_ms"])
        self.assertIn("TimeoutError", result["samples"][0]["error"])

    async def test_foreign_output_fails_even_when_run_completed(self):
        original = ScriptedFactory.create

        def corrupt(factory, **kwargs):
            agent = original(factory, **kwargs)
            run = agent.run
            agent.run = lambda prompt: run(prompt + "-foreign")
            return agent

        with patch.object(ScriptedFactory, "create", corrupt):
            result = await self.case(requests=1, delay=0)
        self.assertEqual(result["failures"], 1)
        self.assertIn("foreign text deltas", result["samples"][0]["error"])


if __name__ == "__main__":
    unittest.main()
