"""Offline ASGI benchmark: real Web API/scheduler/SQLite, scripted Agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx

from codeagent.context import ContextManager
from codeagent.context.models import RuntimeState
from codeagent.events import TokenTotals
from codeagent.tools import TodoStore
from codeagent.web.api import create_app
from codeagent.web.scheduler import RunScheduler
from codeagent.web.storage import SQLiteRepository


class ScriptedFactory:
    def __init__(self, delay: float, events: int):
        self.delay = delay
        self.events = events
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.timings: dict[str, dict[str, float]] = {}

    def create(self, *, event_emitter, cancellation, **_kwargs):
        factory = self

        class ScriptedAgent:
            def __init__(self):
                self.messages = []
                self.context = ContextManager(state=RuntimeState(), todo_store=TodoStore())

            def run(self, prompt):
                with factory.lock:
                    factory.active += 1
                    factory.peak = max(factory.peak, factory.active)
                    factory.timings[prompt] = {"start": time.perf_counter()}
                try:
                    self.messages.append({"role": "user", "content": prompt})
                    for index in range(factory.events):
                        deadline = time.perf_counter() + factory.delay / factory.events
                        while time.perf_counter() < deadline:
                            cancellation.raise_if_cancelled()
                            time.sleep(min(0.02, max(0, deadline - time.perf_counter())))
                        cancellation.raise_if_cancelled()
                        event_emitter.emit("model.text_delta", {"text": f"{prompt}:{index}"})
                    answer = f"done:{prompt}"
                    self.messages.append({"role": "assistant", "content": answer})
                    return SimpleNamespace(final_text=answer, stop_reason="end_turn", usage=TokenTotals())
                finally:
                    with factory.lock:
                        factory.timings[prompt]["end"] = time.perf_counter()
                        factory.active -= 1

        return ScriptedAgent()


def distribution(values: list[float]) -> dict:
    """Nearest-rank percentiles; empty samples are unavailable, never zero."""
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None}
    ordered = sorted(values)
    return {"count": len(values), **{
        f"p{percentile}_ms": round(ordered[math.ceil(len(ordered) * percentile / 100) - 1] * 1000, 3)
        for percentile in (50, 95, 99)
    }}


def verify_result(repo, run_id, conversation_id, prompt, body, event_count):
    frames = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields and "data" in fields:
            frames.append((fields["event"], json.loads(fields["data"])))
    if not frames or frames[-1][0] != "stream.end":
        raise ValueError("SSE completion marker missing")
    events = [data for kind, data in frames if kind != "stream.end"]
    terminal = frames[-1][1]
    if terminal["run_id"] != run_id or terminal["status"] != "completed":
        raise ValueError(f"Unexpected terminal state: {terminal}")
    if terminal["last_seq"] != len(events) or [event["seq"] for event in events] != list(range(1, len(events) + 1)):
        raise ValueError("SSE sequence gap or duplicate")
    if any(event["run_id"] != run_id or event["conversation_id"] != conversation_id for event in events):
        raise ValueError("SSE crossed run/conversation boundaries")
    deltas = [data["payload"]["text"] for kind, data in frames if kind == "model.text_delta"]
    if deltas != [f"{prompt}:{index}" for index in range(event_count)]:
        raise ValueError("Missing, reordered, or foreign text deltas")
    messages = repo.list_messages(conversation_id)
    if [message.content for message in messages] != [prompt, f"done:{prompt}"]:
        raise ValueError("Conversation message isolation failed")
    checkpoint = repo.get_checkpoint_for_run(run_id)
    if checkpoint is None or checkpoint.conversation_id != conversation_id:
        raise ValueError("Checkpoint missing or assigned to another conversation")
    return len(events)


async def run_case(root: Path, workers: int, concurrency: int, args) -> dict:
    root.mkdir()
    workspace = root / "workspace"
    workspace.mkdir()
    repo = SQLiteRepository(root / "state.db")
    factory = ScriptedFactory(args.delay, args.events)
    scheduler = RunScheduler(repo, factory, max_concurrent_runs=workers)
    rows = []
    try:
        app = create_app(repository=repo, scheduler=scheduler, workspace=workspace,
                         env=SimpleNamespace(data_dir=root / "runtime"), static_dir=root / "no-dist")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                (await client.get("/api/health")).raise_for_status()
                # Setup is excluded from measured load. Every request owns a fresh conversation.
                conversations = []
                for index in range(args.requests):
                    response = await client.post("/api/conversations", json={"title": f"load-{index}"})
                    response.raise_for_status()
                    conversations.append(response.json()["id"])
                jobs = iter(enumerate(conversations))

                async def execute(index, conversation_id, row):
                    prompt = f"load-{index}"
                    response = await client.post(f"/api/conversations/{conversation_id}/runs",
                                                 json={"content": prompt, "readOnly": True})
                    row["http_status"] = response.status_code
                    row["submit_seconds"] = time.perf_counter() - row["started"]
                    response.raise_for_status()
                    run_id = response.json()["run_id"]
                    row["run_id"] = run_id
                    stream = await client.get(f"/api/runs/{run_id}/events")
                    stream.raise_for_status()
                    # ASGITransport buffers SSE: this measures completion, not time to first token.
                    row["completion_seconds"] = time.perf_counter() - row["started"]
                    row["event_count"] = verify_result(repo, run_id, conversation_id, prompt, stream.text, args.events)
                    timing = factory.timings[prompt]
                    row["admission_to_agent_seconds"] = timing["start"] - row["started"]
                    row["agent_seconds"] = timing["end"] - timing["start"]
                    row["ok"] = True

                async def user():
                    for index, conversation_id in jobs:
                        row = {"index": index, "conversation_id": conversation_id,
                               "started": time.perf_counter(), "ok": False}
                        try:
                            await asyncio.wait_for(execute(index, conversation_id, row), timeout=args.timeout)
                        except Exception as exc:
                            row["error"] = f"{type(exc).__name__}: {exc}"
                            if "run_id" in row:
                                scheduler.cancel(row["run_id"])
                        finally:
                            row["observed_seconds"] = time.perf_counter() - row.pop("started")
                            rows.append(row)

                cpu_start = time.process_time()
                started = time.perf_counter()
                await asyncio.gather(*(user() for _ in range(min(concurrency, args.requests))))
                elapsed = time.perf_counter() - started
                cpu_seconds = time.process_time() - cpu_start
    finally:
        scheduler.stop()
        repo.close()
    successful = [row for row in rows if row["ok"]]
    result = {
        "workers": workers, "concurrency": concurrency, "requests": args.requests,
        "successes": len(successful), "failures": len(rows) - len(successful),
        "failure_rate": (len(rows) - len(successful)) / args.requests,
        "peak_active_agents": factory.peak,
        "capacity_respected": factory.peak <= workers,
        "elapsed_seconds": round(elapsed, 3), "cpu_seconds": round(cpu_seconds, 3),
        "successful_runs_per_second": round(len(successful) / elapsed, 3),
        "submit": distribution([row["submit_seconds"] for row in rows if "submit_seconds" in row]),
        "completion": distribution([row["completion_seconds"] for row in successful]),
        "admission_to_agent": distribution([row["admission_to_agent_seconds"] for row in successful]),
        "agent": distribution([row["agent_seconds"] for row in successful]),
        "observed_all": distribution([row["observed_seconds"] for row in rows]),
        "samples": sorted(rows, key=lambda row: row["index"]),
    }
    (root / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def positive_int_list(value):
    try:
        values = [int(part) for part in value.split(",")]
        if not values or any(item < 1 for item in values) or len(values) != len(set(values)):
            raise ValueError
        return values
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use distinct positive integers separated by commas") from exc


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=positive_int_list, default=[4])
    parser.add_argument("--concurrency", type=positive_int_list, default=[1, 4, 8, 16])
    parser.add_argument("--requests", type=int, default=32, help="Runs per matrix cell")
    parser.add_argument("--delay", type=float, default=0.25, help="Simulated I/O seconds per Agent")
    parser.add_argument("--events", type=int, default=10, help="Text delta events per Agent")
    parser.add_argument("--timeout", type=float, default=60, help="Per-run deadline including queue time")
    parser.add_argument("--output", type=Path, default=Path("eval-results"))
    args = parser.parse_args()
    if args.requests < 1 or args.events < 1 or not math.isfinite(args.delay) or args.delay < 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("requests/events must be positive; delay finite and nonnegative; timeout finite and positive")
    if max(args.concurrency) > args.requests:
        parser.error("requests must be at least the largest concurrency")
    root = args.output.resolve() / f"web-concurrency-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    root.mkdir(parents=True)
    report = {"mode": "offline-asgi-scripted-agent", "python": platform.python_version(),
              "platform": platform.platform(), "delay_seconds": args.delay, "events_per_agent": args.events,
              "timeout_seconds": args.timeout, "cases": []}
    print(f"Results: {root}", flush=True)
    print("workers clients successes/errors peak runs/s completion_p95_ms admission_p95_ms", flush=True)
    for workers in args.workers:
        for concurrency in args.concurrency:
            result = await run_case(root / f"w{workers}-c{concurrency}", workers, concurrency, args)
            report["cases"].append(result)
            (root / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"{workers:7} {concurrency:7} {result['successes']:6}/{result['failures']:<6} "
                  f"{result['peak_active_agents']:4} {result['successful_runs_per_second']:6.2f} "
                  f"{str(result['completion']['p95_ms']):>17} {str(result['admission_to_agent']['p95_ms']):>16}", flush=True)
    return int(any(case["failures"] or not case["capacity_respected"] for case in report["cases"]))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
