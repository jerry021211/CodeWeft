import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-metrics-"));
const bundle = join(directory, "metrics.cjs");
await build({ entryPoints: [resolve("src/lib/runMetrics.ts")], tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle });
const { collectRunMetrics, runElapsedMs, contextPressure, contextWindowSource } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });
const event = (seq, type, payload = {}, extra = {}) => ({ id: `e${seq}`, seq, type, payload, run_id: "run", agent_id: "root", conversation_id: "conversation", occurred_at: `2026-09-28T00:00:${String(seq).padStart(2, "0")}Z`, ...extra });

test("discovery source, query failures and missing budgets are explicit", () => {
  assert.equal(contextWindowSource(event(1, "context.request_projected", { context_window_source: "model_api" })), "模型接口");
  assert.equal(contextWindowSource(event(1, "context.request_projected", { context_window_reason: "timeout" })), "接口查询超时");
  assert.equal(contextWindowSource(event(1, "context.request_projected", { context_window_reason: "missing_window" })), "接口未返回窗口大小");
  assert.equal(contextPressure(event(1, "context.request_projected", { request_chars: 184196 })).knownBudget, false);
  assert.equal(contextPressure(event(1, "context.request_projected", { request_chars: 184196, max_request_chars: 600000 })).knownBudget, true);
});

test("counts attempted model calls and executed tools once through replay and outcomes", () => {
  const events = [event(1, "run.started"), event(2, "model.started", { call_id: "m" }), event(3, "model.failed", { call_id: "m" }), event(4, "tool.requested", { tool_use_id: "t" }), event(5, "tool.started", { tool_use_id: "t" }), event(6, "tool.failed", { tool_use_id: "t" }), event(7, "tool.blocked", { tool_use_id: "blocked" }), event(8, "tool.started", { tool_use_id: "t" }, { agent_id: "child", parent_agent_id: "root" })];
  const metrics = collectRunMetrics([...events, ...events]);
  assert.equal(metrics.modelCalls, 1);
  assert.equal(metrics.toolCalls, 2);
  assert.equal(metrics.failedTools, 1);
});

test("context takes latest root snapshot and excludes child compactions", () => {
  const metrics = collectRunMetrics([event(2, "context.request_projected", { estimated_total_tokens: 50 }), event(3, "context.compacted"), event(4, "context.request_projected", { estimated_total_tokens: 90 }, { parent_agent_id: "root" }), event(5, "context.compacted", {}, { parent_agent_id: "root" }), event(1, "context.request_projected", { estimated_total_tokens: 10 })]);
  assert.equal(metrics.context.payload.estimated_total_tokens, 50);
  assert.equal(metrics.compactions, 1);
});

test("elapsed time freezes on completion and is unknown for incomplete terminal history", () => {
  const metrics = collectRunMetrics([event(1, "run.started"), event(6, "run.completed")]);
  assert.equal(runElapsedMs(metrics, "completed", Date.now()), 5000);
  const active = collectRunMetrics([event(1, "run.started")]);
  assert.equal(runElapsedMs(active, "running", Date.parse("2026-09-28T00:00:04Z")), 3000);
  assert.equal(runElapsedMs(active, "interrupted", Date.now()), undefined);
});

test("unknown windows do not produce a token percentage; both pressure triggers apply", () => {
  assert.equal(contextPressure(event(1, "context.request_projected", { context_window_tokens: 0, estimated_total_tokens: 100 })).ratio, undefined);
  assert.equal(contextPressure(event(1, "context.request_projected", { context_window_tokens: 100, estimated_total_tokens: 80, near_context_ratio: 0.8 })).near, true);
  assert.equal(contextPressure(event(1, "context.request_projected", { request_chars: 301, compact_threshold_chars: 300 })).near, true);
  assert.equal(contextPressure(event(1, "context.request_blocked")).blocked, true);
});
