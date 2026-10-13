import assert from "node:assert/strict";
import { after, beforeEach, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-approval-status-"));
const bundle = join(directory, "store.cjs");
await build({
  stdin: { contents: 'export { useRunStore } from "./src/store/runStore";', resolveDir: resolve("."), loader: "ts" },
  tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle,
});
const { useRunStore } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });
beforeEach(() => useRunStore.setState({ runs: {} }));

const event = (seq, type, payload = {}) => ({
  id: `event-${seq}`, seq, type, payload, run_id: "run", conversation_id: "conversation",
  agent_id: "root", occurred_at: "2026-10-13T00:00:00Z",
});
const run = () => useRunStore.getState().runs.run;
const request = (seq, id) => event(seq, "approval.requested", { approval_id: id, tool_name: "edit_file" });

for (const status of ["allowed", "denied", "expired"]) {
  test(`${status} event clears waiting status during live delivery and replay`, () => {
    const events = [event(1, "run.started"), request(2, "one"), event(3, `approval.${status}`, { approval_id: "one" })];
    for (const item of events) useRunStore.getState().mergeEvent(item);
    assert.equal(run().approvals.one.status, status);
    assert.equal(run().status, "running");
    useRunStore.setState({ runs: {} });
    useRunStore.getState().mergeEvents(events);
    useRunStore.getState().mergeEvents(events);
    assert.equal(run().status, "running");
  });
}

test("HTTP response and SSE resolve only their approval and wait for the last decision", () => {
  useRunStore.getState().mergeEvents([event(1, "run.started"), request(2, "one"), request(3, "two")]);
  useRunStore.getState().resolveApproval("run", "one", "allowed");
  assert.equal(run().status, "waiting_approval");
  useRunStore.getState().mergeEvent(event(4, "approval.allowed", { approval_id: "one" }));
  assert.equal(run().status, "waiting_approval");
  useRunStore.getState().resolveApproval("run", "two", "allowed");
  assert.equal(run().status, "running");
  useRunStore.getState().mergeEvent(event(5, "approval.allowed", { approval_id: "two" }));
  assert.equal(run().status, "running");
});

test("late decisions do not restart cancelling or finished runs", () => {
  for (const status of ["cancelling", "cancelled", "completed", "failed", "interrupted"]) {
    useRunStore.setState({ runs: {} });
    useRunStore.getState().mergeEvents([request(1, "one"), request(2, "two")]);
    useRunStore.getState().setRunStatus("run", status);
    useRunStore.getState().resolveApproval("run", "one", "allowed");
    useRunStore.getState().mergeEvent(event(3, "approval.expired", { approval_id: "two" }));
    assert.equal(run().status, status);
  }
});
