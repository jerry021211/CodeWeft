import assert from "node:assert/strict";
import { after, beforeEach, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-run-events-"));
const bundle = join(directory, "store.cjs");
await build({
  stdin: {
    contents: 'export { useRunStore } from "./src/store/runStore"; export { createRunEventBuffer } from "./src/lib/runEventBuffer";',
    resolveDir: resolve("."),
    loader: "ts",
  },
  tsconfig: "tsconfig.app.json",
  bundle: true,
  platform: "node",
  format: "cjs",
  outfile: bundle,
});
const { useRunStore, createRunEventBuffer } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });
beforeEach(() => useRunStore.setState({ runs: {} }));

test("cancellation event overrides legacy running payload until terminal confirmation", () => {
  useRunStore.getState().mergeEvents([
    event(1, "run.started", { status: "running" }),
    event(2, "run.cancelling", { status: "running" }),
  ]);
  assert.equal(useRunStore.getState().runs["run-a"].status, "cancelling");
  useRunStore.getState().mergeEvents([event(3, "run.cancelled", { status: "cancelled" })]);
  assert.equal(useRunStore.getState().runs["run-a"].status, "cancelled");
});

test("parallel children keep independent status, results and reused tool ids through replay", () => {
  const child = (seq, agent, type, payload) => ({ ...event(seq, type, payload), agent_id: agent, parent_agent_id: "root" });
  const events = [
    child(1, "a", "subagent.queued", { subagent_id: "a", status: "queued", description: "read A" }),
    child(2, "b", "subagent.running", { subagent_id: "b", status: "running", description: "read B" }),
    child(3, "a", "tool.started", { tool_use_id: "same", name: "read_file" }),
    child(4, "b", "tool.started", { tool_use_id: "same", name: "read_file" }),
    child(5, "b", "tool.completed", { tool_use_id: "same", output: "B" }),
    child(6, "a", "subagent.cancelling", { subagent_id: "a", status: "cancelling" }),
    child(7, "b", "subagent.completed", { subagent_id: "b", status: "completed", result: "report B", duration_ms: 200 }),
    child(8, "b", "subagent.delivered", { subagent_id: "b", status: "completed", result: "report B", duration_ms: 200 }),
  ];
  useRunStore.getState().mergeEvents([...events].reverse());
  useRunStore.getState().mergeEvents(events);
  const run = useRunStore.getState().runs["run-a"];
  assert.equal(run.agents.a.runtime_status, "cancelling");
  assert.equal(run.agents.a.status, "waiting");
  assert.equal(run.agents.b.status, "completed");
  assert.equal(run.agents.b.result, "report B");
  assert.equal(run.actions["a::same"].status, "running");
  assert.equal(run.actions["b::same"].output, "B");
  assert.equal(run.events.length, 8);
});

function event(seq, type = "model.text.delta", payload = { delta: "x" }, runId = "run-a") {
  return { id: `${runId}:${seq}`, seq, type, payload, run_id: runId, conversation_id: `conversation-${runId}`, agent_id: "root", occurred_at: "2026-09-17T00:00:00Z" };
}

test("context snapshots remain available without creating phantom running actions", () => {
  useRunStore.getState().mergeEvents([event(1, "context.request_projected", { estimated_total_tokens: 100 }), event(2, "context.request_blocked", { estimated_total_tokens: 200 })]);
  const run = useRunStore.getState().runs["run-a"];
  assert.equal(run.events.length, 2);
  assert.equal(run.actionOrder.length, 0);
});

test("large replay publishes once while retaining text, actions, usage and final status", () => {
  let updates = 0;
  const unsubscribe = useRunStore.subscribe(() => updates++);
  const events = [event(1, "run.started", {}), event(2, "tool.started", { tool_call_id: "tool-1", tool_name: "read_file" })];
  for (let seq = 3; seq <= 3502; seq++) events.push(event(seq));
  events.push(event(3503, "tool.completed", { tool_call_id: "tool-1", output: "done" }));
  events.push(event(3504, "usage.updated", { call_id: "call-1", input_tokens: 8, output_tokens: 9 }));
  events.push(event(3505, "run.completed", { usage: { input_tokens: 8, output_tokens: 9 } }));
  useRunStore.getState().mergeEvents(events);
  unsubscribe();
  const run = useRunStore.getState().runs["run-a"];
  assert.equal(updates, 1);
  assert.equal(run.events.length, events.length);
  assert.equal(run.lastSeq, 3505);
  assert.equal(run.streamingText, "x".repeat(3500));
  assert.equal(run.status, "completed");
  assert.equal(run.actions[run.actionOrder[0]].output, "done");
  assert.equal(run.actions[run.actionOrder[0]].status, "completed");
  assert.equal(run.usage.input_tokens, 8);
  assert.equal(run.usage.output_tokens, 9);
});

test("reconnect duplicates by id or sequence are ignored without notifying subscribers", () => {
  useRunStore.getState().mergeEvents([event(1), event(2)]);
  const before = useRunStore.getState();
  let updates = 0;
  const unsubscribe = useRunStore.subscribe(() => updates++);
  useRunStore.getState().mergeEvents([event(1), { ...event(2), id: "different-id" }]);
  unsubscribe();
  assert.equal(useRunStore.getState(), before);
  assert.equal(updates, 0);
});

test("batches preserve snapshots and sort out-of-order history", () => {
  useRunStore.getState().mergeEvent(event(2));
  const before = useRunStore.getState().runs["run-a"];
  useRunStore.getState().mergeEvents([event(1), event(3), event(3)]);
  assert.deepEqual(before.events.map(item => item.seq), [2]);
  assert.equal(before.streamingText, "x");
  const after = useRunStore.getState().runs["run-a"];
  assert.deepEqual(after.events.map(item => item.seq), [1, 2, 3]);
  assert.equal(after.streamingText, "xxx");
});

test("events for different conversations stay isolated", () => {
  useRunStore.getState().mergeEvents([event(1), event(1, "model.text.delta", { delta: "other" }, "run-b"), event(2)]);
  assert.equal(useRunStore.getState().runs["run-a"].streamingText, "xx");
  assert.equal(useRunStore.getState().runs["run-b"].streamingText, "other");
});

test("live events flush on a short timer; completion and switching flush immediately", (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const batches = [];
  const buffer = createRunEventBuffer(events => batches.push(events));
  buffer.push(event(1));
  buffer.push(event(2));
  context.mock.timers.tick(31);
  assert.equal(batches.length, 0);
  context.mock.timers.tick(1);
  assert.deepEqual(batches[0].map(item => item.seq), [1, 2]);
  buffer.push(event(3));
  buffer.flush();
  assert.deepEqual(batches[1].map(item => item.seq), [3]);
  context.mock.timers.tick(32);
  buffer.flush();
  assert.equal(batches.length, 2);
});

test("flushing before stream end preserves the authoritative terminal status", (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const buffer = createRunEventBuffer(events => useRunStore.getState().mergeEvents(events));
  buffer.push(event(1, "run.started", {}));
  buffer.push(event(2));
  buffer.flush();
  useRunStore.getState().setRunStatus("run-a", "completed");
  context.mock.timers.tick(32);
  assert.equal(useRunStore.getState().runs["run-a"].status, "completed");
  assert.equal(useRunStore.getState().runs["run-a"].lastSeq, 2);
});


test("Lead terminal usage cannot overwrite worker totals, including reconnect replay", () => {
  const events = [
    event(1, "usage.updated", { call_id: "lead", input_tokens: 1, cache_read_input_tokens: 9, output_tokens: 2 }),
    { ...event(2, "usage.updated", { call_id: "worker", input_tokens: 90, cache_read_input_tokens: 10, output_tokens: 3 }), agent_id: "worker", parent_agent_id: "root" },
    event(3, "run.completed", { usage: { input_tokens: 1, cache_read_input_tokens: 9, output_tokens: 2 } }),
    event(4, "usage.updated", { call_id: "worker", input_tokens: 90, cache_read_input_tokens: 10, output_tokens: 3 }),
  ];
  useRunStore.getState().mergeEvents(events);
  useRunStore.getState().mergeEvents(events);
  const run = useRunStore.getState().runs["run-a"];
  assert.equal(run.usage.input_tokens, 91);
  assert.equal(run.usage.output_tokens, 5);
  assert.equal(run.usage.cache_hit_ratio, 19 / 110);
  assert.equal(run.usageByCall.length, 2);
  // Workers can keep producing calls after the planning run finishes.
  useRunStore.getState().mergeEvents([event(5, "usage.updated", { call_id: "retry", input_tokens: 5 })]);
  assert.equal(useRunStore.getState().runs["run-a"].usage.input_tokens, 96);
});

test("legacy terminal-only usage remains readable", () => {
  useRunStore.getState().mergeEvents([event(1, "run.completed", { usage: { input_tokens: 40, output_tokens: 5 } })]);
  assert.equal(useRunStore.getState().runs["run-a"].usage.input_tokens, 40);
});
