"""Freeze engines, run bounded workers, retain evidence and generate a report."""
from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
import platform
import random
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

from evals.evidence import file_hash, read_json, snapshot, write_json
from . import VERSION
from .dataset import digest, initialize_target, load_bundle, read_jsonl, write_jsonl
from .scoring import TOKEN_FIELDS, grade_run


def records(path):
    if not path.exists():
        return []
    # A killed worker may leave a partial final JSONL line; keep earlier evidence.
    import json
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def usage_evidence(trial: Path, *, offline: bool):
    requests, responses = records(trial / "model-requests.jsonl"), records(trial / "model-responses.jsonl")
    ids = [r["request_index"] for r in requests]
    response_ids = [r["request_index"] for r in responses]
    complete = bool(ids) and not offline and len(set(ids)) == len(ids) and len(set(response_ids)) == len(response_ids) and set(ids) == set(response_ids)
    complete = complete and all(isinstance(r.get("usage"), dict) and all(
        type(r["usage"].get(k)) is int and r["usage"][k] >= 0 for k in TOKEN_FIELDS) for r in responses)
    events = records(trial / "events.jsonl")
    return {"usage_complete": complete,
            "usage": {k: sum(r["usage"][k] for r in responses) if complete else None for k in TOKEN_FIELDS},
            "model_calls": len(requests), "provider_models": sorted({r["model"] for r in responses if r.get("model")}),
            "tool_calls": sum(e.get("type") == "tool.started" for e in events)}


def target_hashes(target):
    return {p.relative_to(target).as_posix(): file_hash(p) for p in target.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(target).parts}


def environment(source: Path):
    # Read only connection settings; never serialize credentials or load runtime flags.
    env = dict(os.environ)
    source = source.resolve()
    for directory in (source, *source.parents):
        if (directory / ".env").is_file():
            from dotenv import dotenv_values
            values = dotenv_values(directory / ".env")
            for key in ("API_KEY", "ANTHROPIC_API_KEY", "BASE_URL", "ANTHROPIC_BASE_URL", "MODEL_ID"):
                # Match the application's load_dotenv(override=False), then
                # resolve the same canonical-name-first aliases as from_env().
                if key not in env and values.get(key) is not None:
                    env[key] = values[key]
            break
    for canonical, alias in (("API_KEY", "ANTHROPIC_API_KEY"), ("BASE_URL", "ANTHROPIC_BASE_URL")):
        value = env.get(canonical) or env.get(alias)
        if value:
            # Frozen v2 workers accept these normalized names already.
            env[canonical] = value
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    return env


def run(bundle: Path, output: Path, *, source: Path, variant="baseline", mode="offline", split="dev",
        repeats=1, model=None, tool_factory=None, timeout=120):
    if repeats < 1 or timeout <= 0:
        raise ValueError("repeats and timeout must be positive")
    if (variant == "enhanced") != bool(tool_factory):
        raise ValueError("Enhanced requires --tool-factory; baseline must not supply one")
    manifest, _, _ = load_bundle(bundle)
    if manifest.get("version") != VERSION:
        raise ValueError("This bundle uses an older tool profile. Use the code-retrieval-v2 bundle or prepare a new bundle.")
    public = [q for q in read_jsonl(bundle / "queries.jsonl") if q["split"] == split]
    env = environment(source)
    model = model or (env.get("MODEL_ID") if mode == "live" else "offline-scripted")
    if mode == "live":
        missing = []
        if not model:
            missing.append("MODEL_ID/--model")
        if not env.get("API_KEY"):
            missing.append("API_KEY or ANTHROPIC_API_KEY")
        if missing:
            raise ValueError("Live mode is missing: " + ", ".join(missing)
                             + ". Read from the process environment or nearest project/parent .env.")
    endpoint = env.get("BASE_URL") or "https://api.anthropic.com"
    address = urlsplit(endpoint)
    public_endpoint = urlunsplit((address.scheme, address.netloc.rsplit("@", 1)[-1], address.path, "", ""))
    profile = {"protocol": VERSION, "model": model, "endpoint": public_endpoint, "endpoint_hash": digest(endpoint),
               "max_api_calls": 12, "max_tool_calls": 24, "max_tokens": 4096, "max_total_tokens": 300_000,
               "timeout_seconds": timeout, "setup_timeout_seconds": 90, "context_window_tokens": 131072,
               "context_mode": "off", "memory": False, "subagents": False, "seed_order": 101,
               "sampling": "provider default (current client has no temperature parameter)",
               "python": platform.python_version(), "base_search_tools": ["glob", "grep", "read_file"],
               "dependencies": {name: importlib.metadata.version(name) for name in ("anthropic", "python-dotenv")}}
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    engine_source = bundle / "baseline-engine" if variant == "baseline" else source
    engine = snapshot(engine_source, output / "engine")
    baseline_files, candidate_files = manifest["baseline_engine"]["files"], engine["files"]
    changed = sorted(p for p in baseline_files.keys() | candidate_files.keys() if baseline_files.get(p) != candidate_files.get(p))
    meta = {"version": VERSION, "variant": variant, "mode": mode, "split": split, "repeats": repeats,
            "profile": profile, "engine_hash": engine["sha256"], "engine_changed_files": changed,
            "tool_factory": tool_factory, "corpus_hash": manifest["corpus_hash"], "queries_hash": manifest["queries_hash"],
            "workspace_valid": True, "state": "running"}
    write_json(output / "run.json", meta)
    target = output / "workspace"
    shutil.copytree(bundle / "target", target)
    initialize_target(target)
    env["PYTHONPATH"] = str(output / "engine")
    # Clear PYTHONHOME so the explicit interpreter/engine cannot be redirected.
    env.pop("PYTHONHOME", None)
    trials = [(q, repeat) for repeat in range(1, repeats + 1) for q in public]
    random.Random(profile["seed_order"]).shuffle(trials)
    predictions = []
    for number, (q, repeat) in enumerate(trials, 1):
        trial = output / "trials" / f"{q['_id']}-r{repeat}"
        trial.mkdir(parents=True)
        write_json(trial / "trial.json", {"query": q["text"], "workspace": str(target), "profile": profile,
                                           "mode": mode, "tool_factory": tool_factory})
        started, observed_query_start, timed_out = time.monotonic(), None, False
        with (trial / "stdout.log").open("w", encoding="utf-8") as stdout, (trial / "stderr.log").open("w", encoding="utf-8") as stderr:
            child = subprocess.Popen([sys.executable, "-P", "-B", "-m", "evals.code_retrieval.worker", "--trial", str(trial)],
                                     cwd=target, env=env, stdout=stdout, stderr=stderr)
            try:
                while child.poll() is None:
                    now = time.monotonic()
                    if observed_query_start is None and (trial / "started.json").exists():
                        observed_query_start = now
                    elapsed = now - (observed_query_start or started)
                    if elapsed > (timeout if observed_query_start else profile["setup_timeout_seconds"]):
                        timed_out = True
                        child.kill()
                        child.wait(timeout=10)
                        break
                    time.sleep(0.1)
            except BaseException:
                child.kill()
                child.wait(timeout=10)
                meta["state"] = "interrupted"
                write_json(output / "run.json", meta)
                raise
        returned = records(trial / "worker-result.jsonl")
        prediction = returned[-1] if returned else {"outcome": "timeout" if timed_out else "worker_error", "answer": None,
                                                   "duration_ms": round((time.monotonic() - observed_query_start) * 1000, 3) if observed_query_start else None,
                                                   "setup_ms": None}
        if timed_out:
            prediction["outcome"] = "timeout" if observed_query_start else "setup_timeout"
        prediction.update(case_id=q["_id"], repeat=repeat, **usage_evidence(trial, offline=mode == "offline"))
        predictions.append(prediction)
        write_jsonl(output / "predictions.jsonl", predictions)
        meta["workspace_valid"] = target_hashes(target) == manifest["target_hashes"]
        write_json(output / "run.json", meta)
        print(f"[{number}/{len(trials)}] {q['_id']}/r{repeat}: {prediction['outcome']}", flush=True)
        if prediction.get("error"):
            print(f"  {prediction.get('error_type')}: {prediction['error']}", flush=True)
        if not meta["workspace_valid"]:
            raise ValueError("Evaluation source changed; stop and inspect the trial before rerunning")
    meta["state"] = "finished"
    meta["provider_models"] = sorted({model for row in predictions for model in row["provider_models"]})
    write_json(output / "run.json", meta)
    grade_run(bundle, output, output / "report")
    return output / "report/report.md"
