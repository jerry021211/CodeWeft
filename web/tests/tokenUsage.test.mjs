import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-token-usage-"));
const bundle = join(directory, "usage.cjs");
await build({ stdin: { contents: `export * from './src/components/TokenUsagePanel';
export { createElement } from 'react'; export { renderToStaticMarkup } from 'react-dom/server';`, resolveDir: resolve(".") },
  tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle });
const { TokenUsagePanel, teamUsageForRun, createElement, renderToStaticMarkup } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });
const render = props => renderToStaticMarkup(createElement(TokenUsagePanel, props));
const team = {
  team: { id: "team", root_run_id: "root", lead_agent_id: "lead", state: "running" },
  agents: [{ id: "worker", name: "后端成员", role: "teammate" }],
  usage: { total_tokens: 900, model_calls: 6, cache_hit_ratio: 0.75, unavailable_calls: 1,
    by_model: { flash: {}, pro: {} },
    by_agent: { lead: { total_tokens: 300, model_calls: 2 }, worker: { total_tokens: 600, model_calls: 4 } } },
};

test("Team total and member breakdown ignore the smaller current Lead run", () => {
  const html = render({ team, run: { usage: { total_tokens: 5 }, usageByCall: [] } });
  assert.match(html, /团队累计/);
  assert.match(html, />900</);
  assert.match(html, /主 Agent/);
  assert.match(html, /后端成员/);
  assert.match(html, /600 · 4 次/);
  assert.match(html, /75.0%/);
  assert.match(html, /flash、pro/);
  assert.match(html, /1 次调用尚无 usage/);
  assert.match(html, /已记录 6 次/);
});

test("Team usage remains visible after planning SSE ends and without a live run", () => {
  assert.equal(teamUsageForRun({ runId: "later", events: [], status: "completed" }, team), team);
  assert.equal(teamUsageForRun(undefined, team), team);
  assert.match(render({ team }), />900</);
});

test("finished Team does not replace unrelated single-agent usage", () => {
  const finished = { ...team, team: { ...team.team, state: "completed" } };
  const run = { runId: "single", events: [], usage: { total_tokens: 7 }, usageByCall: [] };
  assert.equal(teamUsageForRun(run, finished), undefined);
  assert.match(render({ run }), /本轮累计/);
  assert.match(render({ run }), />7</);
  assert.equal(teamUsageForRun({ ...run, runId: "root" }, finished), finished);
  assert.equal(teamUsageForRun({ ...run, events: [{ payload: { team_run_id: "team" } }] }, finished), finished);
});

test("legacy snapshots without breakdown render with explicit missing details", () => {
  assert.match(render({ team: { ...team, usage: { total_tokens: 9 } } }), /暂无 Agent 明细/);
});
