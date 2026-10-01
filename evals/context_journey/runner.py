"""Snapshot-isolated continuous task trials, reusing clients, pricing and seals."""
from __future__ import annotations

from pathlib import Path
import random
import subprocess
import sys
import time
from urllib.parse import urlsplit

from codeagent.context import ContextConfig
from evals.context_suite.materials import context_settings
from evals.context_suite.runner import credentials, new_directory
from evals.context_suite.token_report import build_token_report, token_report_lines
from evals.context_suite.cost_report import build_cost_report, cost_report_lines
from evals.evidence import hashes, read_json, seal, snapshot, write_json
from evals.runner import ROOT, source_metadata
from evals.tracing import tracing_settings
from evals.context_journey.cases import MATERIAL_VERSION, TITLES, material
from evals.context_journey.grading import grade


def plan(*, mode="offline", cases=None, variants=("A", "D"), repeats=1, log_chars=48000, batches=4,
         max_iterations=8, max_tokens=2048, max_api_calls=64, max_trials=12, max_total_api_calls=768,
         timeout=600, order_seed=20260923, **ignored):
    selected = list(TITLES) if cases is None else list(cases)
    if not selected or len(set(selected)) != len(selected) or any(c not in TITLES for c in selected):
        raise ValueError("Cases must be unique T01..T06")
    if mode not in {"live", "offline"} or not variants or len(set(variants)) != len(variants) or any(v not in {"A", "B", "C", "D"} for v in variants):
        raise ValueError("Invalid mode or variants")
    if min(repeats, max_iterations, max_tokens, max_api_calls, max_trials, max_total_api_calls, timeout) <= 0:
        raise ValueError("Budgets must be positive")
    if not 1000 <= log_chars <= 70000 or not 2 <= batches <= 8:
        raise ValueError("log_chars must be 1000..70000 and batches 2..8")
    schedule = [{"case": case, "variant": variant, "repeat": repeat} for repeat in range(1, repeats + 1)
                for case in selected for variant in variants]
    if len(schedule) > max_trials or len(schedule) * max_api_calls > max_total_api_calls:
        raise ValueError("Matrix exceeds trial/call caps; reduce selection or explicitly raise caps")
    random.Random(order_seed).shuffle(schedule)
    return {"version": "context-journey-v1", "material_version": MATERIAL_VERSION,
            "mode": mode, "schedule": schedule, "trials": len(schedule),
            "planned_api_call_cap": len(schedule) * max_api_calls, "planned_wall_seconds_cap": len(schedule) * timeout,
            "log_chars": log_chars, "batches": batches, "order_seed": order_seed, "quality_measurement": mode == "live",
            "limits": {"max_iterations_per_turn": max_iterations, "max_tokens_per_main_request": max_tokens,
                       "max_api_calls_per_trial": max_api_calls, "timeout_per_trial": timeout, "max_total_tokens": 0}}


def write_report(root, results, mode):
    tokens = build_token_report(root, results, mode)
    costs = build_cost_report(tokens)
    lines = ["# 连续任务保持评测", "", f"模式：{mode}。每场是同一 Agent/会话的多个真实用户回合。",
             f"材料版本：{MATERIAL_VERSION}。启动背景由独立准备回合读取；不与旧版材料直接合并统计。",
             "离线响应为脚本注入，只验证执行链路；真实模式也只覆盖小型纯函数项目，不能外推大型编码任务。",
             "自动通过 = 最终功能/状态字段 + 授权操作检查通过；不等于所有自然语言和调查行为都已人工确认。",
             "压缩覆盖单独统计：完整读取固定背景批次，且 C/D 在首次交付请求前已完成至少两次摘要、一次更新，并折叠关键早期要求。",
             "A/B 不要求摘要。覆盖不足不算摘要记忆证据，任务失败和资源消耗仍保留。", "",
             "| 组 | 自动通过/总场次 | 覆盖充分/总场次 | 禁止操作尝试 | 平均秒/场 |",
             "|---|---:|---:|---:|---:|"]
    for variant in "ABCD":
        subset = [r for r in results if r["variant"] == variant]
        if subset:
            lines.append(f'| {variant} | {sum(r["task_success"] for r in subset)}/{len(subset)} | '
                         f'{sum(r["memory_evidence_eligible"] for r in subset)}/{len(subset)} | '
                         f'{sum(len(r["forbidden_attempts"]) for r in subset)} | '
                         f'{sum(r["execution"].get("worker_wall_ms", 0) for r in subset) / len(subset) / 1000:.1f} |')
    lines += ["",
             "| 案例/组/重复 | 自动检查 | 压缩/材料覆盖 | 摘要次数 | 回读尝试 | 秒 | 证据 |",
             "|---|---|---|---:|---:|---:|---|"]
    for r in results:
        label = "通过" if r["task_success"] else "失败：" + ", ".join(r["failed_checks"])
        covered = "充分" if r["memory_evidence_eligible"] else "不足/前置条件失败"
        seconds = r["execution"].get("worker_wall_ms", 0) / 1000
        lines.append(f'| {r["case_id"]}/{r["variant"]}/{r["repeat"]} | {label} | {covered} | '
                     f'{r["coverage"]["successful_summaries"]} | {r["recall_attempts"]} | {seconds:.1f} | '
                     f'[{r["trial_directory"]}]({r["trial_directory"]}/result.json) |')
    skips = [(r, skip) for r in results for skip in r.get("compaction_preflight_skips", [])]
    if skips:
        lines += ["", "摘要调用前预检：以下检查未调用摘要模型，不触发失败冷却，也不计为成功摘要。",
                  "最多可省是假设摘要和交接包装完全不占空间的乐观上限，不是实际节省量。",
                  "| 案例/组/重复 | 阶段 | 候选切点 | 必须保留的用户消息字符 | 最多可省字符 | 最低要求字符 |",
                  "|---|---|---:|---:|---:|---:|"]
        for r, skip in skips:
            lines.append(f'| {r["case_id"]}/{r["variant"]}/{r["repeat"]} | {skip.get("phase")} | '
                         f'{skip["candidate_cut"]} | {skip["protected_user_chars"]} | '
                         f'{skip["max_possible_saved_chars"]} | {skip["required_saved_chars"]} |')
    lines += ["", "所有场次另有 manual-review.json：重复搜索是待核查候选，不自动定罪，也不把无人审查记为无偏移。",
              "T03 须先真实修复解析器并通过测试；T04 须先完成修改；T06 须先接收事件日志。前置失败不能归为摘要遗忘。",
              "T06 若精确字段已在摘要/近期消息中，直接回答有效；否则需人工结合实际请求和归档回读核查来源，不能单凭字面命中归因。",
              "本套没有强制压缩，没有断电/重启测试。各阶段保存 SQLite 检查点，但保存成功不等于已验证重启。",
              "同案例同重复的初始源码、材料和用户阶段相同；自主工具轨迹自然分叉。A/D 资源配对不意味着过程质量已等价。"]
    lines += token_report_lines(tokens) + cost_report_lines(costs)
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "manual-review.md").write_text(
        "# 人工检查（不要改已封存原始证据）\n\n复制各场 manual-review.json 到另一个目录再填写。\n"
        "1. 查看 phases.jsonl 的每段最终答复，再按需查看该阶段 messages.json。\n"
        "2. 检查重复调查是否对应新问题/状态变化，不能只按重复次数判错。\n"
        "3. 检查自然语言是否把未运行测试说成通过；external-verification.json 是评测端事后检查，不是 Agent 的测试证据。\n"
        "4. 对 T03 检查旧问题是否被重新列为待办；对 T05 检查总体开发是否被当成当前交付。\n"
        "5. 对 T06 对照 model-requests.jsonl、messages.json 和归档，判断精确字段是否已可见、补查是否定向。\n"
        "6. 记录具体消息/请求编号和理由；拿不准写待定，不填已通过。\n", encoding="utf-8")
    write_json(root / "result.json", {"version": "context-journey-v1", "material_version": MATERIAL_VERSION,
        "mode": mode, "quality_measurement": mode == "live",
        "all_automatic_checks_passed": all(r["task_success"] for r in results), "trials": results,
        "all_coverage_sufficient": all(r["memory_evidence_eligible"] for r in results), "no_drift_verified": None,
        "token_accounting": tokens, "cost_estimates": costs})


def run_suite(*, output=ROOT / "eval-results", **options):
    settings = plan(**options)
    mode, limits = settings["mode"], settings["limits"]
    env = credentials(mode)
    trace = tracing_settings(env)
    root = new_directory(Path(output), f'journey-{mode}')
    engine = root / "engine-snapshot"
    frozen = snapshot(ROOT, engine)
    write_json(root / "source-manifest.json", {**source_metadata(), "engine_snapshot": frozen})
    write_json(root / "suite.json", settings)
    print(f'LangSmith tracing: {trace["enabled"]}; project={trace["project"]}; trials={settings["trials"]}', flush=True)
    results = []
    for index, item in enumerate(settings["schedule"], 1):
        trial = root / f'trial-{index:03d}-{item["case"]}-{item["variant"]}-r{item["repeat"]}'
        trial.mkdir()
        seed, gold = material(item["case"], log_chars=settings["log_chars"], batches=settings["batches"])
        write_json(trial / "seed.json", seed)
        write_json(trial / "gold.json", gold)
        workspace = trial / "workspace"
        for name, text in seed["workspace_files"].items():
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        context = context_settings(item["variant"], "stress")
        context.update(context_window_tokens=0, summary_context_window_tokens=0, summary_max_chars=ContextConfig().summary_max_chars)
        model = env["MODEL_ID"] if mode == "live" else "offline-journey-script"
        profile = {"suite": "context-journey-v1", "mode": mode, "model": model,
                   "summary_model": (env.get("SUMMARIZATION_MODEL_ID") or model) if mode == "live" else model,
                   "context": context, "max_iterations": limits["max_iterations_per_turn"],
                   "max_tokens": limits["max_tokens_per_main_request"], "max_total_tokens": 0,
                   "max_api_calls": limits["max_api_calls_per_trial"], "wall_timeout_seconds": limits["timeout_per_trial"],
                   # Keep the locked baseline writable at the harness level: a
                   # forbidden attempt must be measured, not prevented by gold.
                   "allowed_writes": list(seed["workspace_files"]) + ["reports/candidate.json"],
                   "provider_host": urlsplit(env.get("BASE_URL") or "https://api.anthropic.com").hostname if mode == "live" else None,
                   "sdk_retries": 0, "recovery_retries": 1, "tracing": trace,
                   "isolation": "WorkspaceGuard, fixed tools and pure-function checker; not an OS sandbox"}
        write_json(trial / "manifest.json", {"case_id": item["case"], "variant": item["variant"], "repeat": item["repeat"],
                   "scale": "stress", "profile": profile, "engine_sha256": frozen["sha256"], "initial_files": hashes(workspace),
                   "seed_history_sha256": seed["input_sha256"], "input_hash_basis": "initial files + public scripted user phases + materials"})
        child_env = {**env, "PYTHONPATH": str(engine), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
                     "CODEAGENT_DATA_DIR": str(trial / "runtime")}
        start = time.monotonic()
        with (trial / "stdout.txt").open("w", encoding="utf-8") as stdout, (trial / "stderr.txt").open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen([sys.executable, "-P", "-B", "-m", "evals.context_journey.worker", "--trial", str(trial)],
                                    cwd=workspace, env=child_env, stdout=stdout, stderr=stderr)
            try:
                code = proc.wait(timeout=limits["timeout_per_trial"])
                execution = read_json(trial / "worker-result.json") if (trial / "worker-result.json").exists() else {"execution_status": "worker_crash"}
                if code:
                    execution.update(execution_status="worker_crash", returncode=code)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                execution = {"execution_status": "timeout", "deadline_seconds": limits["timeout_per_trial"]}
        execution["worker_wall_ms"] = round((time.monotonic() - start) * 1000, 3)
        write_json(trial / "execution.json", execution)
        result = grade(trial, execution)
        results.append(result)
        write_json(root / "partial-results.json", results)
        print(f'{index}/{settings["trials"]} {item["case"]}/{item["variant"]}/r{item["repeat"]}: '
              f'passed={result["task_success"]}, coverage={result["memory_evidence_eligible"]}', flush=True)
    write_report(root, results, mode)
    seal(root)
    return root
