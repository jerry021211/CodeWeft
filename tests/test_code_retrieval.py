"""Tests of scoring invariants and the offline pipeline, never model quality."""
from copy import deepcopy
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evals.code_retrieval.dataset import load_bundle, prepare, source_files, validate_source
from evals.code_retrieval.runner import environment, run
from evals.code_retrieval.pricing import price_usage
from evals.code_retrieval.scoring import compare, compare_reports, grade_case, score_predictions, validate_labels
from evals.evidence import read_json


DOCS = [{"_id": name, "path": name + ".py", "symbol": name, "start_line": 1, "end_line": 2,
         "text": f"def {name}():\n    return 42"} for name in ("a", "b", "c")]


def case(identifier="X", absent=False):
    return {"id": identifier, "category": "absent" if absent else "cross_file", "groups": [] if absent else [
        {"id": name, "members": [{"doc_id": name, "evidence_lines": [2]}]} for name in ("a", "b")]}


def citation(name="a", line=2, quote="return 42"):
    return {"path": name + ".py", "symbol": name, "line": line, "quote": quote}


def prediction(identifier="X", results=None, status="found", outcome="completed"):
    return {"case_id": identifier, "repeat": 1, "outcome": outcome,
            "answer": {"status": status, "results": results if results is not None else [citation()]}}


class CodeRetrievalScoringTests(unittest.TestCase):
    def test_rank_metrics_and_duplicate_penalty(self):
        result = grade_case(case(), prediction(results=[citation("c"), citation("a"), citation("a"), citation("b")]), DOCS)
        self.assertEqual((result["hit1"], result["hit5"], result["mrr5"], result["complete5"]), (0, 1, .5, 1))
        self.assertAlmostEqual(result["ndcg5"], (1 / math.log2(3) + 1 / math.log2(5)) / (1 + 1 / math.log2(3)))
        self.assertEqual(result["irrelevant_returned"], 2)
        self.assertEqual(len(result["review_required"]), 1)

    def test_bad_quotes_and_function_header_do_not_score(self):
        for item in (citation(quote="invented"), citation(line=1, quote="def a():"), citation(line=900)):
            with self.subTest(item=item):
                self.assertEqual(grade_case(case(), prediction(results=[item]), DOCS)["hit5"], 0)

    def test_partial_cross_file_is_not_complete(self):
        result = grade_case(case(), prediction(), DOCS)
        self.assertEqual((result["hit5"], result["group_recall5"], result["complete5"]), (1, .5, 0))

    def test_missing_timeout_and_abstention_denominators(self):
        cases = [case(), *(case(f"Z{i}", absent=True) for i in range(4))]
        rows = [prediction("Z0"), prediction("Z1", [], "not_found"),
                prediction("Z2", [], "not_found", "timeout")]
        m = score_predictions(cases, rows, DOCS)["metrics"]
        self.assertEqual((m["trials"], m["positive_trials"], m["hit5"]), (5, 1, 0))
        self.assertEqual((m["false_positive_rate"], m["correct_abstention_count"], m["incomplete_absent_count"]), (.25, 1, 2))

    def test_only_first_five_results_count(self):
        result = grade_case(case(), prediction(results=[citation("c")] * 5 + [citation()]), DOCS)
        self.assertEqual(result["hit5"], 0)

    def test_duplicate_trials_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            score_predictions([case()], [prediction(), prediction()], DOCS)

    def test_unknown_usage_stays_unknown_and_cache_input_counts(self):
        p = prediction()
        p.update(usage_complete=True, usage={"input_tokens": 10, "output_tokens": 5,
                                           "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20})
        m = score_predictions([case()], [p], DOCS)["metrics"]
        self.assertEqual(m["input_token_median"], 130)
        m = score_predictions([case()], [], DOCS)["metrics"]
        self.assertIsNone(m["usage_totals"]["input_tokens"])

    def test_changed_comparison_conditions_are_rejected(self):
        a = {**score_predictions([case()], [prediction()], DOCS), "version": "v1", "gold_hash": "gold",
             "meta": {"profile": {"model": "test"}, "corpus_hash": "source", "queries_hash": "questions",
                      "split": "test", "repeats": 1, "mode": "live", "provider_models": ["pinned-model"]}}
        b = deepcopy(a)
        self.assertEqual(compare_reports(a, b)["deltas"]["hit5"], 0)
        for key in a["meta"]:
            altered = deepcopy(b)
            altered["meta"][key] = "changed"
            with self.subTest(key=key), self.assertRaises(ValueError):
                compare_reports(a, altered)

    def test_revised_gold_must_point_to_real_lines(self):
        labels = [case()]
        validate_labels(labels, DOCS)
        labels[0]["groups"][0]["members"][0]["evidence_lines"] = [200]
        with self.assertRaises(ValueError):
            validate_labels(labels, DOCS)

    def test_old_tool_profile_cannot_silently_run(self):
        with patch("evals.code_retrieval.runner.load_bundle", return_value=({"version": "code-localization-v1"}, [], [])):
            with self.assertRaisesRegex(ValueError, "older tool profile"):
                run(Path("unused-bundle"), Path("unused-output"), source=Path.cwd())


class CodeRetrievalPricingTests(unittest.TestCase):
    def priced_prediction(self, **changes):
        p = prediction()
        p.update(usage_complete=True, usage={"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                                           "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 200_000})
        p.update(changes)
        return p

    def test_user_rates_and_cache_write_mapping(self):
        parts = price_usage(self.priced_prediction()["usage"])
        self.assertEqual(parts, {"cache_hit_input": .02, "cache_miss_input": 1.2, "output": 4.0, "total": 5.22})
        m = score_predictions([case()], [self.priced_prediction()], DOCS)["metrics"]
        self.assertEqual(m["cost"], 5.22)
        self.assertEqual(m["cost_average_per_trial"], 5.22)

    def test_tiny_charges_are_preserved_and_zero_is_known(self):
        usage = dict.fromkeys(("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"), 0)
        self.assertEqual(price_usage(usage)["total"], 0)
        usage["cache_read_input_tokens"] = 1
        self.assertEqual(price_usage(usage)["total"], .00000002)

    def test_failed_tasks_are_billed_missing_tasks_are_not_zero(self):
        p = self.priced_prediction(outcome="timeout")
        m = score_predictions([case()], [p], DOCS)["metrics"]
        self.assertEqual(m["cost"], 5.22)
        m = score_predictions([case(), case("Y")], [p], DOCS)["metrics"]
        self.assertIsNone(m["cost"])
        self.assertIsNone(m["cost_average_per_trial"])
        self.assertEqual(m["cost_known_subtotal"], 5.22)
        self.assertEqual(m["cost_measured_trials"], 1)
        self.assertFalse(m["cost_complete"])

    def test_incomplete_usage_is_not_billed_as_complete(self):
        for changes in ({"usage_complete": False}, {"usage": {"input_tokens": 100}}):
            with self.subTest(changes=changes):
                result = score_predictions([case()], [self.priced_prediction(**changes)], DOCS)
                self.assertIsNone(result["rows"][0]["cost"])
                self.assertIsNone(result["metrics"]["cost_known_subtotal"])

    def test_comparison_cost_delta_and_rate_consistency(self):
        a = {**score_predictions([case()], [self.priced_prediction()], DOCS),
             "version": "v2", "gold_hash": "gold", "meta": {"mode": "live"}}
        p = self.priced_prediction()
        p["usage"] = {k: v // 2 for k, v in p["usage"].items()}
        b = {**score_predictions([case()], [p], DOCS), "version": "v2", "gold_hash": "gold", "meta": {"mode": "live"}}
        compared = compare_reports(a, b)
        self.assertEqual(compared["deltas"]["cost"], -2.61)
        self.assertEqual(compared["cost_change_percent"], -50)
        altered = deepcopy(b)
        altered["pricing"]["per_million_tokens"]["output"] = "8"
        with self.assertRaisesRegex(ValueError, "pricing"):
            compare_reports(a, altered)


class CodeRetrievalEnvironmentTests(unittest.TestCase):
    def test_project_anthropic_names_are_normalized_for_frozen_worker(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            (root / ".env").write_text("MODEL_ID=test-model\nANTHROPIC_API_KEY=fake-key\nANTHROPIC_BASE_URL=https://example.invalid/anthropic\n", encoding="utf-8")
            env = environment(root)
            self.assertEqual(env["MODEL_ID"], "test-model")
            self.assertEqual(env["API_KEY"], "fake-key")
            self.assertEqual(env["BASE_URL"], "https://example.invalid/anthropic")
            self.assertNotIn("API_KEY", os.environ)

    def test_same_name_environment_wins_and_canonical_alias_order_matches_app(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("MODEL_ID=file-model\nAPI_KEY=file-key\nANTHROPIC_API_KEY=file-alias\nBASE_URL=https://file.invalid\n", encoding="utf-8")
            with patch.dict(os.environ, {"MODEL_ID": "process-model", "API_KEY": "process-key", "ANTHROPIC_API_KEY": "process-alias", "BASE_URL": "https://process.invalid"}, clear=True):
                env = environment(root)
                self.assertEqual((env["MODEL_ID"], env["API_KEY"], env["BASE_URL"]),
                                 ("process-model", "process-key", "https://process.invalid"))
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "process-alias"}, clear=True):
                self.assertEqual(environment(root)["API_KEY"], "file-key")
            with patch.dict(os.environ, {"API_KEY": ""}, clear=True):
                self.assertEqual(environment(root)["API_KEY"], "file-alias")

    def test_nearest_parent_dotenv_loads_only_connection_settings(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            child = root / "project"
            child.mkdir()
            (root / ".env").write_text("MODEL_ID=parent-model\nANTHROPIC_API_KEY=fake-parent\nENABLE_MEMORY=true\nLANGSMITH_TRACING=true\n", encoding="utf-8")
            env = environment(child)
            self.assertEqual(env["API_KEY"], "fake-parent")
            self.assertNotIn("ENABLE_MEMORY", env)
            self.assertEqual(env["LANGSMITH_TRACING"], "false")
            (child / ".env").write_text("MODEL_ID=nearest-model\n", encoding="utf-8")
            env = environment(child)
            self.assertEqual(env["MODEL_ID"], "nearest-model")
            self.assertNotIn("API_KEY", env)


class CodeRetrievalPipelineTests(unittest.TestCase):
    def test_source_labels_freeze_worker_report_and_comparison(self):
        source = Path(__file__).resolve().parents[1]
        validation = validate_source(source)
        self.assertEqual((validation["cases"], validation["dev_cases"]), (24, 4))
        self.assertEqual(validation["test_categories"], {"exact": 4, "natural": 8, "cross_file": 4, "absent": 4})
        self.assertFalse(any(p.name.startswith("test_code_retrieval") for p in source_files(source)))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = prepare(source, root / "bundle")
            manifest, docs, cases = load_bundle(bundle)
            validate_labels(cases, docs)
            self.assertFalse((bundle / "target/evals").exists())
            with patch("evals.code_retrieval.runner.shutil.which", return_value=None) as executable_lookup:
                report = run(bundle, root / "smoke", source=source)
                executable_lookup.assert_not_called()
            for schema_file in (root / "smoke/trials").glob("*/tool-schemas.json"):
                self.assertEqual({s["name"] for s in read_json(schema_file)},
                                 {"glob", "grep", "read_file", "load_tool_output", "load_context_history"})
            data = read_json(report.parent / "result.json")
            self.assertEqual((data["metrics"]["trials"], data["metrics"]["completed_trials"]), (4, 4))
            self.assertEqual(data["metrics"]["hit5"], 0)
            self.assertEqual(data["metrics"]["tool_call_median"], 1)
            self.assertIsNone(data["metrics"]["usage_totals"]["input_tokens"])
            comparison = compare(report.parent, report.parent, root / "comparison")
            self.assertEqual(comparison["deltas"]["hit5"], 0)
            self.assertIn("离线", report.read_text(encoding="utf-8"))
            (bundle / "target/codeagent/__init__.py").write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Frozen bundle changed"):
                load_bundle(bundle)


if __name__ == "__main__":
    unittest.main()
