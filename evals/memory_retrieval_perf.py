"""Repeatable paired candidate-generation timings; synthetic data, no model calls."""
from __future__ import annotations

import argparse
import math
import os
import platform
import random
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from codeagent.memory import MemoryStore
from codeagent.memory.models import MemoryRecord
from evals.evidence import file_hash, seal, write_json


QUERIES = [
    "断线后如何回放事件", "RunScheduler", "重试间隔怎么设置",
    "codeagent/web/scheduler.py", "QUASARZX987", "项目配置规则",
]
TOPICS = [
    ("事件回放", "事件断线后应继续回放，依据序号去重。"),
    ("调度队列", "RunScheduler lives in codeagent/web/scheduler.py. Queue ownership is explicit."),
    ("重试策略", "请求失败后按重试间隔执行，超过次数上限返回错误。"),
    ("项目配置", "项目配置规则：环境变量覆盖默认参数，修改后重启服务。"),
    ("日志约定", "日志包含任务编号与时间，便于排查运行异常。"),
    ("测试约定", "测试使用隔离目录与固定数据，运行后保存检查结果。"),
]


def distribution(values):
    ordered = sorted(values)
    return {
        "n": len(values), "median_ms": round(statistics.median(values), 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * .95) - 1], 3),
        "min_ms": round(ordered[0], 3), "max_ms": round(ordered[-1], 3),
    } if values else {"n": 0}


def measured(fn):
    start = time.perf_counter()
    value = fn()
    return (time.perf_counter() - start) * 1000, value


def populate(root, count, rng):
    root.mkdir(parents=True)
    store = MemoryStore(root)
    for i in range(count):
        title, fact = TOPICS[i % len(TOPICS)]
        record = MemoryRecord(
            name=f"{title}-{i:05}", filename=f"record-{i:05}.md",
            description=f"项目模块 {i} 的{title}",
            content=(fact + f" 模块编号 {i}，适用对应模块。\n") * rng.randint(4, 24),
        )
        (root / record.filename).write_text(store._serialize(record), encoding="utf-8")
    return store


def benchmark(output, count, args):
    rows, builds, verifications, updates = [], [], [], []
    for run in range(args.runs):
        rng = random.Random(args.seed + count + run)
        store = populate(output / f"n{count}-run{run}" / "memory", count, rng)
        elapsed, result = measured(lambda: store.retrieve(QUERIES[0]))
        builds.append({"run": run, "ms": elapsed, "files_read": result.files_read})
        # Warm both paths. No claim of an OS-cold filesystem cache.
        for query in QUERIES:
            store.list_memories()[:50]
            store.retrieve(query)
        queries = (QUERIES * math.ceil(args.samples / len(QUERIES)))[:args.samples]
        rng.shuffle(queries)
        for sample, query in enumerate(queries):
            methods = ["legacy", "indexed"]
            rng.shuffle(methods)
            for method in methods:
                if method == "legacy":
                    elapsed, result = measured(lambda: store.list_memories()[:50])
                    extra = {"candidate_count": len(result)}
                else:
                    elapsed, result = measured(lambda: store.retrieve(query))
                    extra = {
                        "candidate_count": len(result.hits), "backend": result.backend,
                        "files_read": result.files_read,
                        "full_verification": result.full_verification,
                    }
                rows.append({"run": run, "sample": sample, "query": query,
                             "method": method, "ms": elapsed, **extra})
        elapsed, result = measured(lambda: store.retrieve(QUERIES[0], verify_seconds=0))
        verifications.append({"run": run, "ms": elapsed, "files_read": result.files_read})
        path = store.root / "record-00000.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nUPDATEPROBEZX123\n", encoding="utf-8")
        elapsed, result = measured(lambda: store.retrieve("UPDATEPROBEZX123"))
        assert result.files_read == 1
        assert any("UPDATEPROBEZX123" in hit.record.content for hit in result.hits)
        updates.append({"run": run, "ms": elapsed, "files_read": result.files_read})
        print(f"records={count} run={run + 1}/{args.runs} finished", flush=True)
    legacy = distribution([r["ms"] for r in rows if r["method"] == "legacy"])
    indexed = distribution([r["ms"] for r in rows if r["method"] == "indexed"])
    result = {
        "records": count, "legacy": legacy, "indexed": indexed,
        "median_reduction_percent": round(100 * (1 - indexed["median_ms"] / legacy["median_ms"]), 2),
        "per_run": [{"run": run, **{
            method: distribution([r["ms"] for r in rows if r["run"] == run and r["method"] == method])
            for method in ("legacy", "indexed")}} for run in range(args.runs)],
        "periodic_verifications_in_timed_samples": sum(bool(r.get("full_verification")) for r in rows),
        "indexed_backends": sorted({r["backend"] for r in rows if "backend" in r}),
        "first_index_build_samples": builds,
        "forced_full_verification_samples": verifications,
        "single_update_samples": updates,
        "samples": rows,
    }
    write_json(output / f"results-{count}.json", result)
    return {key: value for key, value in result.items() if key != "samples"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("eval-results"))
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--samples", type=int, default=40, help="Queries per method per run")
    parser.add_argument("--seed", type=int, default=20260927)
    args = parser.parse_args()
    if args.runs < 1 or args.samples < 1 or min(args.sizes) < 1 or len(set(args.sizes)) != len(args.sizes):
        parser.error("sizes must be unique positive integers; runs and samples must be positive")
    output = args.output.resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                     + "-memory-perf-" + uuid4().hex[:8])
    output.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1]
    files = [*sorted((source / "codeagent" / "memory").glob("*.py")),
             Path(__file__).resolve(), source / "evals" / "evidence.py"]
    manifest = {}
    for path in files:
        relative = path.relative_to(source)
        manifest[relative.as_posix()] = file_hash(path)
        target = output / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    result = {
        "scope": "Local synthetic candidate-generation benchmark; sequential, filesystem warmed; no model/task quality claims",
        "environment": {"platform": platform.platform(), "processor": platform.processor(),
                        "logical_cpus": os.cpu_count(), "python": platform.python_version(),
                        "sqlite": sqlite3.sqlite_version},
        "protocol": {"runs": args.runs, "samples_per_method_per_run": args.samples,
                     "seed": args.seed, "queries": QUERIES, "candidate_limit": 50,
                     "verify_seconds": 60, "order": "seeded random paired method order",
                     "p95": "nearest rank; descriptive sample percentile, not an SLA"},
        "source_files_sha256": manifest,
        "results": [benchmark(output, count, args) for count in args.sizes],
    }
    # Concurrent product edits would invalidate attribution to the initial snapshot.
    assert all(file_hash(source / relative) == digest for relative, digest in manifest.items())
    write_json(output / "result.json", result)
    seal(output)
    print(f"Evidence: {output}", flush=True)


if __name__ == "__main__":
    main()
