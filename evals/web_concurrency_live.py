"""Paid, real-model read-only concurrency run against a source snapshot."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from codeagent.config import EnvironmentConfig
from codeagent.runtime import RuntimeDataPaths
from codeagent.web.api import create_app
from codeagent.web.factory import WebAgentFactory
from codeagent.web.scheduler import RunScheduler
from codeagent.web.storage import SQLiteRepository
from evals.web_concurrency import distribution


TARGETS = [
    ("codeagent/runtime/parallel.py", "admission", "进程级模型、工具、子任务并发限额如何生效"),
    ("codeagent/runtime/cancellation.py", "CancellationToken", "父子取消传播与首次取消原因的保留"),
    ("codeagent/runtime/execution.py", "RunBudget", "Token 和执行时间预算如何限制运行"),
    ("codeagent/web/scheduler.py", "RunScheduler", "submit、start 和 _worker 如何安排独立会话"),
    ("codeagent/config.py", "EnvironmentConfig", "环境配置如何映射到 Web 并发设置"),
    ("codeagent/events/sink.py", "RecordingEventSink", "事件持久化与模型调用统计如何关联"),
    ("codeagent/hooks/loop_guard.py", "LoopGuard", "如何识别重复失败并停止无效循环"),
    ("tests/test_web_concurrency.py", "ConcurrentSchedulerTests", "并发测试如何验证取消与会话隔离"),
]
ANGLES = ["解释关键执行步骤", "分析一个边界条件", "说明线程安全或状态隔离机制", "提出一个有针对性的测试用例"]


def timestamp(value):
    return datetime.fromisoformat(value).timestamp() if value else None


def peak_intervals(intervals):
    edges = [(start, 1) for start, end in intervals if start is not None and end is not None]
    edges += [(end, -1) for start, end in intervals if start is not None and end is not None]
    active = peak = 0
    for _, delta in sorted(edges):
        active += delta
        peak = max(peak, active)
    return peak


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=32)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--model-slots", type=int, default=32)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--output", type=Path, default=Path("eval-results"))
    args = parser.parse_args()
    if min(args.tasks, args.workers, args.model_slots, args.timeout, args.max_tokens) < 1:
        parser.error("All numeric options must be positive")
    # Load configured credentials before changing the output data directory.
    env = EnvironmentConfig.from_env()
    os.environ["CODEAGENT_MAX_CONCURRENT_MODELS"] = str(args.model_slots)
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    root = args.output.resolve() / f"web-live-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1]
    hashes = {}
    for name, _, _ in TARGETS:
        destination = workspace / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
        hashes[name] = hashlib.sha256(destination.read_bytes()).hexdigest()
    # An independent Git boundary prevents the parent's /eval-results/ ignore
    # rule from hiding fixture files from the product's glob/grep tools.
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True, capture_output=True)
    env = replace(env, data_dir=root / "runtime", web_max_concurrent_runs=args.workers,
                  max_tokens=args.max_tokens, stream=True, enable_skills=False,
                  mcp_config_path=root / "no-mcp.json", team_runtime_enabled=False,
                  memory_config=replace(env.memory_config, enabled=False),
                  embedding_config=replace(env.embedding_config, enabled=False),
                  lsp_config=replace(env.lsp_config, enabled=False),
                  context_config=replace(env.context_config, mode="off"),
                  loop_guard_config=replace(env.loop_guard_config, max_active_seconds=args.timeout,
                                            max_total_tokens=100_000),
                  recovery_config=replace(env.recovery_config, max_retries=1,
                                          max_continuations=0, escalated_max_tokens=args.max_tokens))
    repo = SQLiteRepository(root / "state.db")
    factory = WebAgentFactory(env, workspace, repo, data_paths=RuntimeDataPaths(env.data_dir))
    scheduler = RunScheduler(repo, factory, max_concurrent_runs=args.workers, approval_timeout=10)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True).stdout.strip()
    report = {"mode": "live-model-asgi-read-only", "model": env.model_id, "protocol": env.model_protocol,
              "source_revision": revision, "source_hashes": hashes, "tasks": args.tasks,
              "workers": args.workers, "model_slots": args.model_slots, "max_tokens": args.max_tokens,
              "timeout_seconds": args.timeout, "recovery_max_retries": 1, "samples": []}
    (root / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Results: {root}\nModel: {env.model_id}; tasks={args.tasks}; workers={args.workers}; model_slots={args.model_slots}", flush=True)
    try:
        app = create_app(repository=repo, scheduler=scheduler, workspace=workspace, env=env, static_dir=root / "no-dist")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                (await client.get("/api/health")).raise_for_status()
                rows = []
                for index in range(args.tasks):
                    path, symbol, question = TARGETS[index % len(TARGETS)]
                    marker = f"BENCH-{index + 1:03}"
                    prompt = (f"只读代码分析任务 {marker}。请实际调用 read_file 读取 {path}，"
                              f"围绕 {symbol} 分析：{question}；重点{ANGLES[(index // len(TARGETS)) % len(ANGLES)]}。"
                              "这是独立窄任务，只依据指定文件内的定义回答，不追踪外部调用方或其他文件。"
                              "优先一次 read_file 读取相关片段，必要时补读，最多读取3次，然后直接回答。"
                              "不使用 glob/grep、不运行命令、不修改文件、不委派、不提问。"
                              f"最终用中文给出约200字结论，引用文件路径和具体符号，首行写 {marker}。")
                    response = await client.post("/api/conversations", json={"title": marker})
                    response.raise_for_status()
                    rows.append({"index": index, "marker": marker, "path": path, "symbol": symbol,
                                 "prompt": prompt, "conversation_id": response.json()["id"]})
                started = time.perf_counter()

                async def submit(row):
                    begin = time.perf_counter()
                    response = await client.post(f"/api/conversations/{row['conversation_id']}/runs",
                                                 json={"content": row["prompt"], "readOnly": True})
                    row["submit_ms"] = (time.perf_counter() - begin) * 1000
                    response.raise_for_status()
                    row["run_id"] = response.json()["run_id"]

                submissions = await asyncio.gather(*(submit(row) for row in rows), return_exceptions=True)
                for row, outcome in zip(rows, submissions):
                    if isinstance(outcome, BaseException):
                        row["submission_error"] = type(outcome).__name__
                last_progress = -10
                timed_out = False
                while True:
                    runs = [repo.get_run(row["run_id"]) for row in rows if "run_id" in row]
                    pending = [run for run in runs if scheduler.is_run_pending(run.id)]
                    elapsed = time.perf_counter() - started
                    if elapsed - last_progress >= 10 or not pending:
                        print(f"{elapsed:.1f}s {dict(Counter(run.status for run in runs))}", flush=True)
                        last_progress = elapsed
                    if not pending:
                        break
                    if elapsed > args.timeout and not timed_out:
                        timed_out = True
                        for run in pending:
                            scheduler.cancel(run.id)
                        print("Batch deadline reached; cancelling remaining runs", flush=True)
                    await asyncio.sleep(0.5)
                report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
                calls = []
                run_intervals = []
                for row in rows:
                    if "run_id" not in row:
                        row["ok"] = False
                        continue
                    run = repo.get_run(row["run_id"])
                    events = repo.list_events(run.id)
                    messages = repo.list_messages(run.conversation_id)
                    answer = next((m.content for m in reversed(messages) if m.role == "assistant"), "")
                    tool_events = [e for e in events if e.type == "tool.completed"]
                    model_calls = repo.list_model_calls(run_id=run.id)
                    calls.extend(model_calls)
                    run_intervals.append((timestamp(run.started_at), timestamp(run.finished_at)))
                    row.update(status=run.status, error=run.error, answer=answer,
                               model_calls=len(model_calls), tool_names=[e.payload.get("name") for e in tool_events])
                    row["completion_seconds"] = timestamp(run.finished_at) - timestamp(run.created_at)
                    row["queue_seconds"] = timestamp(run.started_at) - timestamp(run.created_at) if run.started_at else None
                    first = next((e for e in events if e.type == "model.text_delta" and e.payload.get("text")), None)
                    row["first_text_seconds"] = timestamp(first.occurred_at) - timestamp(run.created_at) if first else None
                    stream = await client.get(f"/api/runs/{run.id}/events")
                    stream.raise_for_status()
                    isolation = all(e.conversation_id == row["conversation_id"] and e.run_id == run.id for e in events)
                    checkpoint = repo.get_checkpoint_for_run(run.id)
                    row["checks"] = {"terminal_completed": run.status == "completed", "marker": row["marker"] in answer,
                                     "source_cited": row["path"] in answer, "symbol_cited": row["symbol"] in answer,
                                     "read_tool_completed": any(e.payload.get("name") == "read_file" and e.payload.get("is_error") is not True for e in tool_events),
                                     "isolation": isolation, "checkpoint": checkpoint is not None,
                                     "sse_end": "event: stream.end" in stream.text,
                                     "real_model_usage": any(c.usage_available for c in model_calls)}
                    row["ok"] = all(row["checks"].values())
                    (root / f"task-{row['index'] + 1:03}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
                report["samples"] = rows
                report["successes"] = sum(row.get("ok", False) for row in rows)
                report["statuses"] = dict(Counter(row.get("status", "submit_failed") for row in rows))
                report["peak_running_runs"] = peak_intervals(run_intervals)
                report["peak_model_calls"] = peak_intervals([(timestamp(c.started_at), timestamp(c.completed_at)) for c in calls])
                report["model_calls"] = len(calls)
                report["failed_model_calls"] = sum(c.status == "failed" for c in calls)
                report["usage_available_calls"] = sum(c.usage_available for c in calls)
                report["usage"] = {name: sum(getattr(c, name) or 0 for c in calls if c.usage_available)
                                   for name in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
                report["completion"] = distribution([row["completion_seconds"] for row in rows if row.get("ok")])
                report["first_text"] = distribution([row["first_text_seconds"] for row in rows if row.get("first_text_seconds") is not None])
                report["source_unchanged"] = all(hashlib.sha256((workspace / name).read_bytes()).hexdigest() == digest for name, digest in hashes.items())
                (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({key: value for key, value in report.items() if key not in {"samples", "source_hashes"}}, ensure_ascii=False, indent=2), flush=True)
    finally:
        scheduler.stop()
        repo.close()
    return int(report["successes"] != args.tasks or not report["source_unchanged"])


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
