import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, unlinkSync, rmdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "personal-todos-"));
const bundle = join(directory, "panel.cjs");
await build({
  stdin: { contents: `export { PersonalTodoPanel } from './src/components/PersonalTodoPanel'; export { personalTodoStatus } from './src/lib/personalTodoStatus'; export { createElement } from 'react'; export { renderToStaticMarkup } from 'react-dom/server';`, resolveDir: resolve(".") },
  tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle,
});
const { PersonalTodoPanel, personalTodoStatus, createElement: h, renderToStaticMarkup: render } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });

test("personal progress preserves the supplied sequence and distinguishes all three states", () => {
  const html = render(h(PersonalTodoPanel, { executionStatus: "running", todos: [
    { content: "先调查", status: "completed" },
    { content: "实现功能", status: "in_progress" },
    { content: "验证结果", status: "pending" },
  ] }));
  assert.ok(html.indexOf("先调查") < html.indexOf("实现功能"));
  assert.ok(html.indexOf("实现功能") < html.indexOf("验证结果"));
  assert.match(html, /1 已完成 · 1 进行中 · 1 待处理/);
  assert.match(html, /aria-expanded="true"/);
  for (const status of ["completed", "in_progress", "pending"]) assert.match(html, new RegExp(`data-status="${status}"`));
});

test("stopped or waiting runs retain unfinished steps without a working spinner", () => {
  for (const executionStatus of ["cancelled", "interrupted", "failed", "completed", "queued", "waiting_approval", "cancelling", "review", "paused", "idle"]) {
    const html = render(h(PersonalTodoPanel, { executionStatus, todos: [{ content: "校验结果", status: "in_progress" }] }));
    assert.doesNotMatch(html, /animate-spin/);
    assert.match(html, /1 未完成/);
    assert.match(html, /role="status"/);
    assert.match(html, /校验结果/);
  }
});

test("refresh, cancellation and resumption follow Run identity rather than a stale step", () => {
  const plan = { owner_id: "agent_root", agent_id: "agent_root", run_id: "old", run_status: "cancelled" };
  assert.equal(personalTodoStatus(plan), "cancelled");
  assert.equal(personalTodoStatus(plan, { runId: "old", status: "cancelled" }), "cancelled");
  assert.equal(personalTodoStatus(plan, { runId: "new", status: "running" }), "review");
  assert.equal(personalTodoStatus({ ...plan, run_id: "new" }, { runId: "new", status: "running" }), "running");
  assert.equal(personalTodoStatus(plan, { runId: "new", status: "completed" }), "completed");
});

test("member progress uses its own execution and respects stopped Teams", () => {
  const plan = { owner_id: "session", agent_id: "worker", run_id: "root" };
  const run = { runId: "root", status: "running", agents: { worker: { runtime_status: "completed" } } };
  assert.equal(personalTodoStatus(plan, run), "completed");
  const team = { team: { state: "paused" }, sessions: [{ id: "session", state: "work" }] };
  assert.equal(personalTodoStatus(plan, run, team), "paused");
  assert.equal(personalTodoStatus(plan, run, { ...team, team: { state: "cancelled" } }), "cancelled");
});

test("cleared personal lists disappear and completed lists remain visible", () => {
  assert.equal(render(h(PersonalTodoPanel, { todos: [] })), "");
  const html = render(h(PersonalTodoPanel, { title: "成员甲 · 个人步骤", todos: [{ content: "完成验证", status: "completed" }] }));
  assert.match(html, /成员甲 · 个人步骤/);
  assert.match(html, /1 已完成/);
  assert.match(html, /完成验证/);
});
