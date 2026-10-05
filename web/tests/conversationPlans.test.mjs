import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-plan-message-"));
const bundle = join(directory, "plans.cjs");
await build({
  stdin: { contents: `export { conversationPlans } from './src/lib/conversationPlans';
    export { processAnchors } from './src/lib/conversationProcess';
    export { PlanMessage } from './src/components/PlanMessage';
    export { createElement } from 'react';
    export { renderToStaticMarkup } from 'react-dom/server';`, resolveDir: resolve(".") },
  tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle,
});
const { conversationPlans, processAnchors, PlanMessage, createElement, renderToStaticMarkup } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });
const plan = (overrides = {}) => ({ id: "p1", run_id: "planning-run", revision: 1, status: "submitted",
  title: "SeatFlow 实施方案", markdown: "## 数据一致性\n\n锁座与订单创建在同一事务内完成。", read_only: false, ...overrides });

test("submitted plans stay with their planning turn after execution and history reload", () => {
  const input = [plan({ id: "p2", revision: 2, status: "started", execution_run_id: "execution-run" }),
    plan({ status: "superseded" }), plan({ id: "p3", run_id: "new-run", status: "draft" })];
  const grouped = conversationPlans(input);
  assert.deepEqual(grouped["planning-run"].map(p => p.id), ["p1", "p2"]);
  assert.equal(grouped["execution-run"], undefined);
  assert.equal(grouped["new-run"], undefined);
  assert.equal(input[0].id, "p2");
  const anchors = processAnchors([{ id: "prompt", run_id: "planning-run", role: "user" },
    { id: "execute", run_id: "execution-run", role: "user" }], Object.keys(grouped));
  assert.deepEqual(anchors.after.prompt, ["planning-run"]);
  assert.deepEqual(anchors.trailing, []);
});

test("conversation shows the full submitted Markdown and a clear completion state", () => {
  const html = renderToStaticMarkup(createElement(PlanMessage, { plan: plan(), current: true, onDecision() {} }));
  assert.match(html, /方案已完成，等待确认/);
  assert.match(html, /<h2>数据一致性<\/h2>/);
  assert.match(html, /锁座与订单创建在同一事务内完成/);
  assert.match(html, /批准并执行/);
  assert.match(html, /修改方案/);
});

test("historical and already started plans cannot present an execution action", () => {
  for (const props of [{ plan: plan(), current: false }, { plan: plan({ status: "started" }), current: true }]) {
    const html = renderToStaticMarkup(createElement(PlanMessage, { ...props, onDecision() {} }));
    assert.doesNotMatch(html, /批准并执行|修改方案/);
    assert.match(html, /锁座与订单创建/);
  }
});

test("read-only or busy conversations disable inline approval", () => {
  for (const props of [{ plan: plan({ read_only: true }) }, { plan: plan(), busy: true }]) {
    const html = renderToStaticMarkup(createElement(PlanMessage, { ...props, current: true, onDecision() {} }));
    assert.match(html, /<button[^>]*disabled=""[^>]*>批准并执行/);
  }
});

test("an accidental modification request can be undone without approving execution", () => {
  const html = renderToStaticMarkup(createElement(PlanMessage, { plan: plan({ status: "rejected" }), current: true, onDecision() {} }));
  assert.match(html, /取消修改，恢复原方案/);
  assert.match(html, /恢复后仍需确认才会执行/);
  assert.doesNotMatch(html, /批准并执行/);
  const historical = renderToStaticMarkup(createElement(PlanMessage, { plan: plan({ status: "rejected" }), current: false, onDecision() {} }));
  assert.doesNotMatch(historical, /取消修改，恢复原方案/);
});
