"""Offline memory diagnostics, not an LLM benchmark or product retriever.

Run: python -B -m evals.memory_research
Only synthetic records are read. Each run retains its fixtures and evidence in
a new output directory. No environment configuration or model SDK is loaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import statistics
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from codeagent.memory import MemoryConfig, MemoryManager, MemoryStore
from codeagent.memory.manager import _memory_selection_prompt, _recent_message_text
from codeagent.memory.models import MemoryRecord
from codeagent.memory.store import _terms
from codeagent.models import ModelResponse
from evals.evidence import file_hash, seal, write_json


class ScriptedSelector:
    """Return explicitly supplied filenames; never estimate model quality."""

    def __init__(self, filenames: list[str]) -> None:
        self.filenames = filenames
        self.requests: list[dict] = []

    def create_message(self, **kwargs):
        self.requests.append(kwargs)
        return ModelResponse(
            stop_reason="end_turn",
            content=[{"type": "text", "text": json.dumps({
                "selected_memories": self.filenames,
            })}],
        )


def seed(root: Path, records: list[MemoryRecord]) -> MemoryStore:
    """Build a versioned synthetic fixture without timing index rebuilds."""
    root.mkdir(parents=True, exist_ok=False)
    store = MemoryStore(root)
    for record in records:
        filename = record.filename
        if not filename or Path(filename).name != filename or not filename.endswith(".md"):
            raise ValueError("Synthetic fixture requires a plain markdown filename")
        (root / filename).write_text(store._serialize(record), encoding="utf-8")
    return store


def measure(fn, repeats: int = 5) -> dict:
    fn()  # Warm up; these are not cold-filesystem measurements.
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    return {"median_ms": round(statistics.median(samples), 3),
            "samples_ms": [round(item, 3) for item in samples]}


def select(store, requested, query, config=None):
    client = ScriptedSelector(requested)
    context = MemoryManager(store, config or MemoryConfig(retrieval_mode="legacy")).select_context(
        [{"role": "user", "content": query}], client=client,
        model="offline-scripted-selector", max_tokens=800,
    )
    return context, client


def candidate_probe(root: Path, count: int) -> dict:
    target = MemoryRecord(name="z-target", filename="z-target.md",
                          description="Routing convention", content="NEEDLE_ROUTING uses route_v2.")
    records = [MemoryRecord(name=f"a-note-{i:04}", filename=f"a-note-{i:04}.md",
                           description=f"Unrelated topic {i}", content="Background filler. " * 30)
               for i in range(count - 1)] + [target]
    store = seed(root / f"candidate-{count}", records)
    listed = store.list_memories()
    candidates = listed[:MemoryConfig().max_items_in_prompt]
    context, client = select(store, [target.filename], "Which convention handles NEEDLE_ROUTING?")
    # Positive control: same target becomes eligible when candidate cap is widened.
    expanded, _ = select(store, [target.filename], "Which convention handles NEEDLE_ROUTING?",
                         MemoryConfig(max_items_in_prompt=count, retrieval_mode="legacy"))
    result = {
        "records": count, "target_position_1based": [r.filename for r in listed].index(target.filename) + 1,
        "candidate_count": len(candidates),
        "target_in_candidates": target.filename in [r.filename for r in candidates],
        "scripted_out_of_candidate_choice_injected": target.filename in context,
        "expanded_cap_positive_control_injected": target.filename in expanded,
        "manual_search_ids": [r.filename for r in store.legacy_search("NEEDLE_ROUTING")],
        "selection_calls": len(client.requests),
        "list_memories_warm": measure(store.list_memories),
        "manual_search_warm": measure(lambda: store.legacy_search("NEEDLE_ROUTING")),
    }
    assert not result["target_in_candidates"]
    assert not result["scripted_out_of_candidate_choice_injected"]
    assert result["expanded_cap_positive_control_injected"]
    assert target.filename in result["manual_search_ids"]
    return result


def search_probe(root: Path) -> list[dict]:
    store = seed(root / "search", [
        MemoryRecord(name="SSE Policy", filename="sse-policy.md", description="事件回放约定",
                     content="事件断线后应继续回放，依据序号去重。"),
        MemoryRecord(name="Retry Policy", filename="retry-policy.md", description="Network failures",
                     content="Retry requests with exponential backoff."),
        MemoryRecord(name="Scheduler", filename="scheduler.md", description="Queue ownership",
                     content="RunScheduler schedules queued jobs."),
    ])
    cases = [
        ("zh-keyword", "事件", ["sse-policy.md"]),
        ("zh-sentence", "断线后如何回放事件", ["sse-policy.md"]),
        ("zh-spaced", "事件 回放", ["sse-policy.md"]),
        ("en-words", "exponential backoff", ["retry-policy.md"]),
        ("code-symbol", "RunScheduler", ["scheduler.md"]),
        ("semantic-paraphrase", "重新执行的等待时间怎么设置", ["retry-policy.md"]),
        ("no-relevant", "QUASAR_UNRELATED_937", []),
        ("empty-query", "", []),
    ]
    results = []
    for case_id, query, relevant in cases:
        returned = [r.filename for r in store.legacy_search(query, max_items=5)]
        results.append({"id": case_id, "query": query, "terms": _terms(query),
                        "relevant_ids": relevant, "returned_ids": returned,
                        "recall_at_5": len(set(returned) & set(relevant)) / len(relevant) if relevant else None})
    # These assert diagnostic reproduction, not desirable future behavior.
    assert results[0]["recall_at_5"] == 1
    assert results[1]["recall_at_5"] == 0
    assert results[2]["recall_at_5"] == 1
    return results


def selection_probe(root: Path) -> dict:
    record = MemoryRecord(name="Generic Convention", filename="generic-convention.md",
                          description="Project conventions", content="BODY_ONLY_ROUTE_KEY uses route_v2.")
    store = seed(root / "selection", [record])
    prompt = _memory_selection_prompt(store.list_memories(), [{"role": "user", "content": "Explain routing"}])
    catalog = json.loads(prompt.split("长期记忆清单：\n", 1)[1])
    _, unrelated = select(store, [], "QUASAR_UNRELATED_937")
    _, empty = select(MemoryStore(root / "nonexistent-empty-memory"), [], "Any task")
    tail_marker = "CURRENT_REQUEST_TAIL_MARKER"
    task = "Ordinary background. " * 100 + tail_marker
    preview = _recent_message_text([{"role": "user", "content": task}])
    short_preview = _recent_message_text([{"role": "user", "content": tail_marker}])
    result = {
        "selection_catalog_fields": sorted(catalog[0]),
        "body_only_key_exposed_in_catalog": "BODY_ONLY_ROUTE_KEY" in json.dumps(catalog),
        "long_request_chars": len(task), "request_preview_chars": len(preview),
        "long_request_tail_visible": tail_marker in preview,
        "short_request_tail_visible": tail_marker in short_preview,
        "unrelated_nonempty_store_selection_calls": len(unrelated.requests),
        "empty_store_selection_calls": len(empty.requests),
    }
    assert not result["body_only_key_exposed_in_catalog"]
    assert not result["long_request_tail_visible"] and result["short_request_tail_visible"]
    assert result["unrelated_nonempty_store_selection_calls"] == 1
    assert result["empty_store_selection_calls"] == 0
    return result


def injection_probe(root: Path) -> dict:
    store = seed(root / "injection", [
        MemoryRecord(name="Large", filename="large.md", description="Large relevant record", content="x" * 1000),
        MemoryRecord(name="Small", filename="small.md", description="Small relevant record", content="Keep stable IDs."),
    ])
    requested = ["large.md", "small.md"]
    config = MemoryConfig(session_budget_chars=350, retrieval_mode="legacy")
    context, _ = select(store, requested, "Apply both conventions", config)
    injected = [filename for filename in requested if f'file="{filename}"' in context]
    result = {"scripted_selected_ids": requested, "injected_ids": injected,
              "budget_chars": config.session_budget_chars, "injected_chars": len(context)}
    assert injected == ["small.md"] and len(context) <= config.session_budget_chars
    return result


def fts_probe() -> dict:
    result = {"sqlite_version": sqlite3.sqlite_version, "tokenizers": {}}
    with sqlite3.connect(":memory:") as connection:
        for tokenizer in ("unicode61", "trigram"):
            try:
                connection.execute(f"CREATE VIRTUAL TABLE ft_{tokenizer} USING fts5(body, tokenize='{tokenizer}')")
                connection.execute(f"INSERT INTO ft_{tokenizer}(body) VALUES (?)", ("事件断线后应继续回放",))
                queries = {}
                for term in ("事件", "断线后", "事件断线后应继续回放"):
                    queries[term] = connection.execute(
                        f"SELECT count(*) FROM ft_{tokenizer} WHERE body MATCH ?", (term,)
                    ).fetchone()[0]
                result["tokenizers"][tokenizer] = {"available": True, "query_match_counts": queries}
            except sqlite3.OperationalError as exc:
                result["tokenizers"][tokenizer] = {"available": False, "error": str(exc)}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("eval-results"))
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output.resolve() / f"{stamp}-memory-research-{uuid4().hex[:8]}"
    output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[1]
    source_files = [source / "codeagent" / "agent.py", source / "codeagent" / "models.py",
                    source / "codeagent" / "messages.py", source / "evals" / "evidence.py",
                    Path(__file__).resolve()]
    source_files += sorted((source / "codeagent" / "memory").glob("*.py"))
    manifest = {path.relative_to(source).as_posix(): file_hash(path) for path in source_files}
    # Retain the studied implementation so later product edits do not erase evidence.
    for path in source_files:
        target = output / "source" / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source,
                          capture_output=True, text=True, check=False)
    result = {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Legacy candidate/search control only; synthetic fixtures; scripted selector; no provider or end-to-end agent evaluation",
        "python": platform.python_version(), "git_head": head.stdout.strip() if head.returncode == 0 else None,
        "source_files_sha256": manifest,
        "source_manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
        "default_config": {key: str(value) if isinstance(value, Path) else value
                           for key, value in asdict(MemoryConfig()).items()},
        "candidate_probes": [candidate_probe(output, count) for count in (100, 1000)],
        "search_probes": search_probe(output),
        "selection_probe": selection_probe(output),
        "injection_probe": injection_probe(output),
        "fts_probe": fts_probe(),
        "diagnostic_assertions_passed": True,
    }
    write_json(output / "result.json", result)
    seal(output)
    # ASCII JSON also works in Windows terminals with a non-UTF-8 code page.
    print(json.dumps({"output": str(output), "result": result}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
