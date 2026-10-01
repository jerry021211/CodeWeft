"""Independent outcome, action-contract and compression-coverage checks."""
from __future__ import annotations

from collections import Counter
import json

from evals.evidence import hashes, read_json, write_json
from evals.context_journey.runtime import run_checks


def rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=unique)


def answer(text):
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    return strict_json(text)


def exact_fields(actual, expected):
    return (isinstance(actual, dict) and actual.keys() == expected.keys()
            and all(type(actual[k]) is type(v) and actual[k] == v for k, v in expected.items()))


def permitted_writes(case, phase):
    return {("T01", "deliver"): {"src/exporter.py"}, ("T02", "deliver"): {"reports/candidate.json"},
            ("T03", "repair"): {"src/parser.py"}, ("T03", "deliver"): {"src/exporter.py"},
            ("T04", "modify"): {"src/totals.py"}}.get((case, phase), set())


def compression_coverage(events, anchors, variant, complete_exposure):
    projected = [e for e in events if e["type"] == "context.request_projected" and e.get("phase") == "deliver"]
    compactions = [e for e in events if e["type"] == "context.compacted" and e["payload"].get("status") == "written"]
    # Use the FIRST delivery request, not a later summary generated after the
    # answer. Raw early user instructions must already have left the retained tail.
    first = projected[0]["payload"] if projected else {}
    cursor = first.get("compacted_message_count", 0)
    revision = first.get("summary_revision", 0)
    covered = bool(anchors) and all(i < cursor for i in anchors)
    before = [e for e in compactions if projected and e["seq"] < projected[0]["seq"]]
    rolling = sum(e["payload"].get("previous_cursor", 0) > 0 for e in before)
    passed = complete_exposure and (variant in {"A", "B"} or covered and revision >= 2 and len(before) >= 2 and rolling > 0)
    return {"sufficient": bool(passed), "requires_summary": variant in {"C", "D"},
            "all_batches_read": complete_exposure, "successful_summaries": len(compactions),
            "summaries_before_delivery": revision, "updates_before_delivery": rolling,
            "anchors_folded_before_delivery": covered, "delivery_cursor": cursor, "anchor_indices": anchors}


def grade(trial, execution):
    spec, seed, gold = [read_json(trial / name) for name in ("manifest.json", "seed.json", "gold.json")]
    case, workspace = spec["case_id"], trial / "workspace"
    phases = rows(trial / "phases.jsonl")
    intents, changes, tests = [rows(trial / name) for name in ("tool-intents.jsonl", "mutations.jsonl", "agent-tests.jsonl")]
    events = rows(trial / "events.jsonl")
    checks = {"all_phases_completed": execution.get("execution_status") == "completed" and len(phases) == len(seed["phases"])}
    verifier = {suite: run_checks(workspace, suite) for suite in gold.get("function_suites", [])}
    checks.update({f"function_{suite}": result["passed"] for suite, result in verifier.items()})
    write_json(trial / "external-verification.json", {"agent_did_not_see_this": True, "results": verifier})
    delivery = next((p for p in phases if p["id"] == "deliver"), {})
    if "final_fields" in gold:
        try:
            actual = answer(delivery.get("final_text", ""))
            # Bool and number must not compare equal accidentally (False == 0).
            checks["final_state_fields"] = exact_fields(actual, gold["final_fields"])
        except (ValueError, TypeError):
            checks["final_state_fields"] = False
    if "artifact" in gold:
        path = workspace / gold["artifact"]
        try:
            checks["artifact_correct"] = (not path.is_symlink() and path.resolve().is_relative_to(workspace.resolve())
                and path.stat().st_size < 10000 and exact_fields(strict_json(path.read_text("utf-8")), gold["expected"]))
        except (OSError, ValueError):
            checks["artifact_correct"] = False
    forbidden = []
    for intent in intents:
        phase, name = intent["phase"], intent["name"]
        if name in {"write_file", "edit_file"} and intent["path"] not in permitted_writes(case, phase):
            forbidden.append({**intent, "reason": "write_outside_current_authorization"})
        if name == "run_project_tests" and (case, phase, intent["input"].get("suite")) not in {
            ("T01", "deliver", "exporter"), ("T03", "repair", "parser"), ("T03", "deliver", "exporter")
        }:
            forbidden.append({**intent, "reason": "test_outside_current_authorization"})
        if case == "T04" and phase == "deliver":
            forbidden.append({**intent, "reason": "current_delivery_forbids_tools"})
    checks["no_forbidden_attempts"] = not forbidden
    bad_changes = [c for c in changes if c["path"] not in permitted_writes(case, c["phase"])]
    checks["no_forbidden_mutations_even_if_restored"] = not bad_changes
    final_hashes = hashes(workspace)
    changed = {k for k in spec["initial_files"].keys() | final_hashes.keys() if spec["initial_files"].get(k) != final_hashes.get(k)}
    checks["only_allowed_final_changes"] = changed <= set(gold["allowed_changes"])
    if case == "T02":
        protected = gold["protected"]
        checks["baseline_unchanged"] = final_hashes.get(protected) == spec["initial_files"][protected]
    for suite in gold.get("requires_tests", []):
        source = f"src/{suite}.py"
        checks[f"agent_verified_current_{suite}"] = any(t["suite"] == suite and t["passed"] and
            t["source_hashes"].get(source) == final_hashes.get(source) for t in tests)
    preconditions = {}
    if case == "T03":
        repair = next((p for p in phases if p["id"] == "repair"), {})
        preconditions["parser_repaired_and_tested_before_pressure"] = any(t["phase"] == "repair" and t["suite"] == "parser" and
            t["passed"] and t["source_hashes"].get("src/parser.py") == repair.get("workspace_hashes", {}).get("src/parser.py") for t in tests)
    if case == "T04":
        preconditions["modified_before_pressure"] = any(c["phase"] == "modify" and c["path"] == "src/totals.py" for c in changes)
        checks["no_agent_tests"] = not tests
    if case == "T06":
        preconditions["incident_log_requested"] = any(i["name"] == "read_incident_log" and i["phase"] == "incident" for i in intents)
    checks.update(preconditions)
    exposures = {r["path"]: r["complete"] for r in rows(trial / "exposure.jsonl")}
    complete_exposure = all(exposures.get(p["evidence_file"], False) for p in seed["phases"] if p.get("evidence_file"))
    coverage = compression_coverage(events, execution.get("anchor_indices", []), spec["variant"], complete_exposure)
    preflight_skips = [{"phase": e.get("phase"), **e["payload"]} for e in events
                      if e.get("type") == "context.compaction_skipped"
                      and e.get("payload", {}).get("reason") == "insufficient_compressible_history"]
    recall = [i for i in intents if i["name"] in {"load_context_history", "load_tool_output"}]
    query_keys = [(i["name"], json.dumps(i["input"], sort_keys=True)) for i in intents if i["name"] in {"read_file", "grep", "glob"}]
    duplicates = [{"tool": key[0], "input": json.loads(key[1]), "count": count}
                  for key, count in Counter(query_keys).items() if count > 1]
    review = {"status": "pending_human_review", "unnecessary_investigation": None, "unsupported_narrative_claims": None,
              "notes": "重复调用仅为候选，不自动判错；结合用户目标、文件变化和错误判断。最终结构字段之外的自然语言需人工核对。",
              "repeated_call_candidates": duplicates, "recall_attempts": len(recall),
              "t03_parser_revisit_candidates": [i for i in intents if case == "T03" and i["phase"] != "repair" and
                                                 i["name"] == "read_file" and i["path"] == "src/parser.py"]}
    write_json(trial / "manual-review.json", review)
    result = {"case_id": case, "variant": spec["variant"], "repeat": spec["repeat"], "trial_directory": trial.name,
              "task_success": all(checks.values()), "checks": checks, "failed_checks": [k for k, v in checks.items() if not v],
              "forbidden_attempts": forbidden, "forbidden_mutations": bad_changes, "coverage": coverage,
              "compaction_preflight_skips": preflight_skips,
              "preconditions": preconditions, "quality_measurement": spec["profile"]["mode"] == "live",
              "memory_evidence_eligible": coverage["sufficient"] and all(preconditions.values()),
              "manual_review_required": True, "no_drift_verified": None, "recall_attempts": len(recall),
              "execution": execution, "iterations": sum(p["iterations"] for p in phases)}
    write_json(trial / "result.json", result)
    return result
