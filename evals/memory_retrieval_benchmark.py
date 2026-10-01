"""Offline retrieval diagnostics and timings; no model-quality claims or API calls."""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from codeagent.memory import MemoryStore
from codeagent.memory.models import MemoryRecord
from evals.evidence import file_hash, seal, write_json


def timing(fn, count=5):
    samples = []
    value = None
    for _ in range(count):
        start = time.perf_counter()
        value = fn()
        samples.append((time.perf_counter() - start) * 1000)
    return {"median_ms": round(statistics.median(samples), 3),
            "samples_ms": [round(value, 3) for value in samples]}, value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("eval-results"))
    args = parser.parse_args()
    output = args.output.resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                      + "-memory-retrieval-" + uuid4().hex[:8])
    root = output / "memory"
    root.mkdir(parents=True, exist_ok=False)
    store = MemoryStore(root)
    for i in range(997):
        record = MemoryRecord(name=f"a-filler-{i:04}", filename=f"a-filler-{i:04}.md",
                              description=f"Unrelated record {i}", content="Filler archive. " * 40)
        (root / record.filename).write_text(store._serialize(record), encoding="utf-8")
    records = [
        MemoryRecord(name="z-events", filename="z-events.md", description="项目约定",
                     content="事件断线后应继续回放，依据序号去重。"),
        MemoryRecord(name="z-scheduler", filename="z-scheduler.md", description="Queue ownership",
                     content="RunScheduler lives in codeagent/web/scheduler.py."),
        MemoryRecord(name="z-target", filename="z-target.md", description="Routing convention",
                     content="NEEDLE_ROUTING must use route_v2."),
    ]
    for record in records:
        (root / record.filename).write_text(store._serialize(record), encoding="utf-8")
    cases = [
        ("tail-exact", "NEEDLE_ROUTING", "z-target.md"),
        ("zh-sentence", "断线后如何回放事件", "z-events.md"),
        ("zh-keyword", "事件", "z-events.md"),
        ("code-symbol", "RunScheduler", "z-scheduler.md"),
        ("code-components", "run scheduler", "z-scheduler.md"),
        ("code-path", "codeagent/web/scheduler.py", "z-scheduler.md"),
        ("no-relevant", "QUASARZX987", None),
        ("cross-language-limit", "重新发送先前未收到的通知", "z-events.md"),
    ]
    legacy_ids = [record.filename for record in store.list_memories()[:50]]
    cold_time, cold = timing(lambda: store.retrieve("NEEDLE_ROUTING"), count=1)
    legacy_time, _ = timing(lambda: store.list_memories()[:50])
    warm_time, warm = timing(lambda: store.retrieve("NEEDLE_ROUTING"))
    results = []
    for case, query, relevant in cases:
        indexed = [hit.record.filename for hit in store.retrieve(query).hits]
        manual = [record.filename for record in store.legacy_search(query)]
        results.append({"id": case, "query": query, "relevant_id": relevant,
                        "legacy_auto_candidate_has_target": relevant in legacy_ids if relevant else None,
                        "legacy_manual_ids": manual, "indexed_ids": indexed,
                        "indexed_has_target": relevant in indexed if relevant else None})
    assert all(item["indexed_has_target"] for item in results[:6])
    assert results[6]["indexed_ids"] == []
    path = root / "z-target.md"
    path.write_text(path.read_text(encoding="utf-8").replace("route_v2", "route_v3"), encoding="utf-8")
    update_time, update = timing(lambda: store.retrieve("NEEDLE_ROUTING"), count=1)
    assert update.files_read == 1 and "route_v3" in update.hits[0].record.content
    source = Path(__file__).resolve().parents[1]
    files = [*sorted((source / "codeagent" / "memory").glob("*.py")), Path(__file__).resolve()]
    manifest = {}
    for path in files:
        relative = path.relative_to(source)
        manifest[relative.as_posix()] = file_hash(path)
        target = output / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    result = {
        "scope": "Synthetic diagnostic corpus; retrieval only; target placement intentional; no LLM or downstream task score",
        "python": platform.python_version(), "records": 1000, "cases": results,
        "source_files_sha256": manifest,
        "cold_index_build_and_query": {**cold_time, "files_read": cold.files_read},
        "legacy_list_first_fifty_warm": legacy_time,
        "indexed_query_warm": {**warm_time, "files_read": warm.files_read, "backend": warm.backend},
        "single_file_update_and_query": {**update_time, "files_read": update.files_read},
    }
    write_json(output / "result.json", result)
    seal(output)
    print(json.dumps({"output": str(output), **result}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
