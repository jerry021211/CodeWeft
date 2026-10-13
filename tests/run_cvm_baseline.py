"""Checked stage-0 test entry point; never silently succeeds on zero tests.

Run from the repository root: python tests/run_cvm_baseline.py
Reports go to ignored eval-results/, while the legacy fixture remains read-only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def run_checked(patterns, *, start_dir=None, stream=None):
    suite = unittest.TestSuite()
    groups = {}

    def identifiers(tests):
        for test in tests:
            if isinstance(test, unittest.TestSuite):
                yield from identifiers(test)
            else:
                yield test.id()

    for pattern in patterns:
        collected = unittest.TestLoader().discover(str(start_dir or REPO / "tests"), pattern=pattern)
        if collected.countTestCases() == 0:
            raise ValueError(f"Zero tests collected for pattern: {pattern}")
        groups[pattern] = set(identifiers(collected))
        suite.addTests(collected)
    result = unittest.TextTestRunner(verbosity=2, stream=stream).run(suite)
    if result.testsRun == 0 or len(result.skipped) == result.testsRun:
        raise ValueError("No tests actually executed (empty or entirely skipped)")
    skipped = {test.id() for test, _reason in result.skipped}
    for pattern, ids in groups.items():
        if ids <= skipped:
            raise ValueError(f"Pattern entirely skipped: {pattern}")
    return result


def main(argv=None, *, stage=0):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", action="append", help="Additional test glob; each must collect tests")
    parser.add_argument("--output", type=Path, help=f"Report JSON path (default: timestamped eval-results/cvm-stage{stage}/)")
    args = parser.parse_args(argv)
    required = ["test_cvm_baseline.py"]
    if stage >= 1:
        required += ["test_cvm_contract*.py", "test_cvm_checkpoint*.py", "test_context_nonteam_regressions.py",
                     "test_prefix_cache.py", "test_plan_mode.py"]
    if stage >= 2:
        required += ['test_cvm_evidence*.py', 'test_cvm_verification*.py', 'test_cvm_snapshot*.py',
                     'test_tool_evidence.py', 'test_parallel_execution.py', 'test_bash_process_guard.py']
    patterns = list(dict.fromkeys([*required, *(args.pattern or [])]))
    harness_files = sorted({Path(__file__), *(p for pattern in patterns for p in (REPO / "tests").glob(pattern))})
    if stage >= 1:
        harness_files.append(REPO / "tests" / f"run_cvm_stage{stage}.py")
    if stage >= 2:
        harness_files.append(REPO / 'tests' / 'cvm_stage2_probe.py')
    source_files = [p for p in (REPO / "codeagent").rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".md", ".txt"}]
    source_files += [REPO / "tests" / "cvm_fixtures.py", REPO / "pyproject.toml"]
    hashes = lambda paths: {p.relative_to(REPO).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    source_before, harness_before = hashes(source_files), hashes(harness_files)
    try:
        result = run_checked(patterns)
    except ValueError as error:
        print(f"Stage {stage} FAILED: {error}", file=sys.stderr)
        return 2
    if not result.wasSuccessful():
        return 1

    from codeagent.cvm import CvmConfig
    from cvm_fixtures import GOLDEN, assess, baseline_config, digest, pack_traces, run_legacy_case as run_case, scenarios, source_manifest
    cases = scenarios()
    config = CvmConfig()
    traces = [run_case(case, cvm=config) for case in cases]
    observations = {case["id"]: assess(case, trace) for case, trace in zip(cases, traces)}
    legacy = json.loads(GOLDEN.read_text(encoding="utf-8"))
    packed = pack_traces(traces)
    compatible = packed == {key: legacy[key] for key in ("traces", "request_blobs")}
    success = compatible and all(item["baseline_observed"] for item in observations.values())
    stage2_probe = None
    if stage >= 2:
        from cvm_stage2_probe import run_probe
        stage2_probe = run_probe()
        success = success and stage2_probe['passed']
    manifest = source_manifest()
    harness_after = hashes(harness_files)
    stable = source_before == manifest["source_sha256"] and harness_before == harness_after
    success = success and stable
    report = {
        "schema_version": 1, "stage": stage, "passed": success, "comparison_mode": "off",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest": manifest, "source_unchanged_during_tests": stable,
        "source_before_sha256": source_before, "harness_sha256": harness_after,
        "legacy_artifact_sha256": hashlib.sha256(GOLDEN.read_bytes()).hexdigest(),
        "legacy_commit": legacy["manifest"]["commit"],
        "tests": {"patterns": patterns, "run": result.testsRun, "skipped": len(result.skipped)},
        "config": baseline_config(), "cvm": config.snapshot(),
        "scenario_set_sha256": digest(cases), "scenarios": cases,
        "prompt_policy": "frozen historical system prompt; current prompt parity tested separately",
        "off_matches_legacy": compatible, "observations": observations, **packed,
        **({'stage2_probe': stage2_probe} if stage2_probe is not None else {}),
    }
    output = args.output or REPO / "eval-results" / f"cvm-stage{stage}" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    if output.resolve() == GOLDEN.resolve():
        parser.error("The legacy baseline is immutable; select a different report path")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Stage {stage} {'PASS' if success else 'FAIL'}: {result.testsRun} tests; "
          f"{len(cases)} offline scenarios; off_matches_legacy={compatible}")
    print(f"Report: {output.resolve()}")
    if not stable:
        print("Source or tests changed during validation; this run cannot certify the current checkout.", file=sys.stderr)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
