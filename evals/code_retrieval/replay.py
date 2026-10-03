"""Offline paired retrieval audit; no model or embedding calls.

Compare pre-change search sources from Git with the working tree, keeping all
other dependencies and the old challenge's target corpus fixed. This measures
retrieval, not the Agent's final answer. Saved rewrites are replayed verbatim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def hashes(root, pattern="*.py"):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob(pattern)) if p.is_file() and "__pycache__" not in p.parts}


def replay_worker(engine, target, output, jobs):
    # No client/config environment loading: remote inference cannot be enabled.
    sys.path.insert(0, str(engine))
    from codeagent.code_search.service import CodeSearch

    rows = []
    for job in read(jobs):
        search = CodeSearch(target, output / "indexes" / job["id"])
        candidate_pool = []
        original_candidates = search.candidates

        def capture_candidates(*args, **kwargs):
            result = original_candidates(*args, **kwargs)
            candidate_pool[:] = [f"{d['path']}::{d['symbol']}@{d['definition_start_line']}" for d in result[0]]
            return result

        search.candidates = capture_candidates
        rewrites = job.get("rewrites", {})
        search.rewrite = lambda query: (rewrites[query], "recorded_replay", 0) if query in rewrites else ([], "unavailable", 0)
        try:
            payload = json.loads(search.search(**job["input"]))
            payload["audit_candidate_pool"] = candidate_pool
            rows.append({"id": job["id"], "payload": payload})
        except ValueError as exc:
            # Invalid historical arguments stay in the audit, never repaired.
            rows.append({"id": job["id"], "error": str(exc)})
    write(output / "results.json", rows)


def build_jobs(bundle, history):
    jobs = [{"id": "raw-" + q["_id"], "case_id": q["_id"], "stratum": "raw_no_rewrite",
             "input": {"query": q["text"]}} for q in lines(bundle / "queries.jsonl") if q["split"] == "test"]
    for trial in sorted((history / "full-after" / "trials").iterdir()):
        rewrites = {}
        responses = {r["request_index"]: r for r in lines(trial / "model-responses.jsonl")}
        for req in lines(trial / "model-requests.jsonl"):
            if "Convert a code search request" in str(req.get("system", "")):
                text = "".join(b.get("text", "") for b in responses[req["request_index"]]["content"])
                rewrites[req["messages"][-1]["content"]] = json.loads(text)["queries"]
        calls = [e["payload"]["input"] for e in lines(trial / "events.jsonl")
                 if e["type"] == "tool.started" and e["payload"].get("name") == "search_code"]
        for i, arguments in enumerate(calls, 1):
            jobs.append({"id": f"recorded-{trial.name}-{i}", "case_id": trial.name.split("-")[0],
                         "stratum": "recorded_tool_calls", "input": arguments, "rewrites": rewrites})
    stage = read(history / "tool-audit" / "direct-replay-stages.json")
    queries = [r["query"] for r in stage["routes"]]
    jobs.append({"id": "diagnostic-C01", "case_id": "C01", "stratum": "known_failure",
                 "input": {"query": queries[0], "path": stage["scope"]},
                 "rewrites": {queries[0]: queries[1:]}})
    return jobs


def score(rows, jobs, bundle):
    from evals.code_retrieval.scoring import citation_documents, grade_case
    docs = citation_documents(bundle / "target", lines(bundle / "corpus.jsonl"))
    cases = {c["id"]: c for c in read(bundle / "gold.json")["cases"]}
    scored = []
    for row, job in zip(rows, jobs, strict=True):
        assert row["id"] == job["id"]
        case = cases[job["case_id"]]
        results = row.get("payload", {}).get("results", [])
        strict = grade_case(case, {"outcome": "completed" if "error" not in row else "failed",
                                  "answer": {"status": "found" if results else "insufficient_evidence", "results": results}}, docs)
        expected = {m["doc_id"] for g in case["groups"] for m in g["members"]}
        identities = [f"{r['path']}::{r['symbol']}@{r['definition_start_line']}" for r in results[:5]]
        pool = row.get('payload', {}).get('audit_candidate_pool', [])
        source_valid = []
        for r in results:
            raw = (bundle / "target" / r["path"]).read_bytes()
            actual = raw.decode("utf-8-sig").splitlines()[r["line"] - 1:r["end_line"]]
            source_valid.append(r["quote"] == "\n".join(actual) and hashlib.sha256(raw).hexdigest() == r["content_hash"])
        scored.append({"id": row["id"], "case_id": case["id"], "category": case["category"],
                       "stratum": job["stratum"], "positive": bool(expected),
                       "error": row.get("error"), "identity_hit1": bool(identities and identities[0] in expected),
                       "identity_hit5": bool(set(identities) & expected),
                       "candidate_hit40": bool(set(pool[:40]) & expected),
                       "candidate_target_ranks": {doc: pool.index(doc) + 1 for doc in sorted(expected) if doc in pool},
                       **{k: strict[k] for k in ("hit1", "hit5", "mrr5", "ndcg5", "group_recall5", "complete5")},
                       "citations": strict["citations"], "unjudged": strict["review_required"],
                       "source_valid": sum(source_valid), "returned": len(source_valid),
                       "scan_complete": row.get("payload", {}).get("scan_complete"),
                       "duration_ms": row.get("payload", {}).get("duration_ms")})
    summaries = {}
    for stratum in sorted({j["stratum"] for j in jobs}):
        subset = [r for r in scored if r["stratum"] == stratum]
        pos = [r for r in subset if r["positive"]]
        summaries[stratum] = {"calls": len(subset), "positive_calls": len(pos),
            "distinct_positive_questions": len({r["case_id"] for r in pos}),
            **{k: sum(r[k] for r in pos) for k in ("identity_hit1", "identity_hit5", "candidate_hit40", "hit1", "hit5", "complete5")},
            "errors": sum(bool(r["error"]) for r in subset),
            "source_valid": sum(r["source_valid"] for r in subset), "returned": sum(r["returned"] for r in subset)}
    return {"summaries": summaries, "rows": scored}


def run(source, bundle, history, output, before_ref):
    # Fail before writing if the historical benchmark seal is broken.
    from evals.code_retrieval.dataset import load_bundle
    load_bundle(bundle)
    output.mkdir(parents=True, exist_ok=False)
    jobs = build_jobs(bundle, history)
    write(output / "jobs.json", jobs)
    target = output / "target"
    shutil.copytree(bundle / "target", target)
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    expected_target = hashes(bundle / "target")
    assert hashes(target) == expected_target
    commit = subprocess.check_output(["git", "rev-parse", before_ref], cwd=source, text=True).strip()
    manifest = {"before_commit": commit, "network_calls": 0, "embedding": "disabled",
                "gold_sha256": hashlib.sha256((bundle / "gold.json").read_bytes()).hexdigest(),
                "target": expected_target, "engines": {},
                "interpretation": "Tool-level regression audit only; raw Chinese queries have no new rewrites. Recorded calls are a selected subset, not Agent outcomes. No absence verdict or semantic-vector quality is scored. Latencies are single cold samples, not a speed benchmark."}
    for arm in ("before", "after"):
        engine = output / arm / "engine"
        for path in (source / "codeagent").rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            dest = engine / path.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        shutil.copytree(source / "codeagent/prompts/templates", engine / "codeagent/prompts/templates")
        if arm == "before":
            for filename in ("chunks.py", "index.py", "service.py"):
                rel = "codeagent/code_search/" + filename
                data = subprocess.check_output(["git", "show", f"{commit}:{rel}"], cwd=source)
                (engine / rel).write_bytes(data)
        manifest["engines"][arm] = hashes(engine, "*")
    write(output / "manifest.json", manifest)
    for arm in ("before", "after"):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--worker",
                        str(output / arm / "engine"), str(target), str(output / arm), str(output / "jobs.json")],
                       check=True, cwd=target, env=env, timeout=240)
        assert hashes(target) == expected_target
    report = {arm: score(read(output / arm / "results.json"), jobs, bundle) for arm in ("before", "after")}
    report["changed"] = [{"id": a["id"], "before": a, "after": b}
                         for a, b in zip(report["before"]["rows"], report["after"]["rows"], strict=True)
                         if any(a[k] != b[k] for k in ("identity_hit1", "identity_hit5", "candidate_hit40", "hit1", "hit5", "complete5", "source_valid"))]
    write(output / "report.json", report)
    print(json.dumps({arm: report[arm]["summaries"] for arm in ("before", "after")}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        replay_worker(*(Path(p).resolve() for p in sys.argv[2:]))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--source", type=Path, default=Path.cwd())
        parser.add_argument("--bundle", type=Path, required=True)
        parser.add_argument("--history", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument("--before-ref", default="HEAD")
        args = parser.parse_args()
        run(args.source.resolve(), args.bundle.resolve(), args.history.resolve(), args.output.resolve(), args.before_ref)
