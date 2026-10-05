import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-composer-team-"));
const bundle = join(directory, "composer.cjs");
await build({ stdin: { contents: `export { ChatWorkspace } from './src/components/ChatWorkspace';
export { ComposerTeamStatus } from './src/components/ComposerTeamStatus';
export { buildRunHistory } from './src/store/runStore';
export { createElement } from 'react'; export { renderToStaticMarkup } from 'react-dom/server';
export { QueryClient, QueryClientProvider } from '@tanstack/react-query';`, resolveDir: resolve(".") },
  tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle });
const { ChatWorkspace, ComposerTeamStatus, buildRunHistory, createElement: h, renderToStaticMarkup: render, QueryClient, QueryClientProvider } = createRequire(import.meta.url)(bundle);
after(() => rmSync(directory, { recursive: true }));
const noop = () => {};
function team(state = "running") {
  return { team: { id: "team", state, root_run_id: "planning-run" }, agents: [], sessions: [
    { id: "old", agent_id: "a", generation: 1, state: "work" },
    { id: "current", agent_id: "a", generation: 2, state: "idle" },
    { id: "active", agent_id: "b", generation: 1, state: "work" },
  ] };
}
function workspace(overrides = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  try {
    return render(h(QueryClientProvider, { client }, h(ChatWorkspace, {
      messages: [], draft: "补充要求", theme: "light", onDraft: noop, onSend: noop, onCancel: noop,
      onApprovalDecision: noop, onReadOnlyChange: noop, onOpenLeft: noop, onOpenRight: noop, onOpenMcp: noop, onToggleTheme: noop,
      run: buildRunHistory("planning-run", "completed", []), team: team(), teamLeadActive: true,
      onPauseTeam: noop, onResumeTeam: noop, ...overrides,
    })));
  } finally { client.clear(); }
}

test("composer shows live Team progress after planning completes without disabling instructions", () => {
  const html = workspace();
  assert.match(html, /aria-label="输入框团队状态"/);
  assert.match(html, /1 位成员工作中/);
  assert.match(html, /暂停团队/);
  assert.match(html, /团队正在执行，可向 Lead 补充指令/);
  assert.doesNotMatch(html.match(/<textarea[^>]*>/)[0], /\sdisabled(?:=|[ >])/);
  assert.doesNotMatch(html.match(/<button[^>]*aria-label="发送"[^>]*>/)[0], /\sdisabled(?:=|[ >])/);
});

test("pausing and paused states have distinct controls and keep the user's draft", () => {
  const stopping = workspace({ team: team("pausing") });
  assert.match(stopping, /等待当前操作退出/);
  assert.doesNotMatch(stopping, /暂停团队<|继续执行</);
  assert.match(stopping.match(/<button[^>]*aria-label="发送"[^>]*>/)[0], /\sdisabled(?:=|[ >])/);
  const paused = workspace({ team: team("paused") });
  assert.match(paused, /继续执行/);
  assert.doesNotMatch(paused, /团队运行中|位成员工作中/);
  assert.match(paused, /补充要求<\/textarea>/);
});

test("finished Teams return to ordinary sending and solo Agent retains stop generation", () => {
  for (const state of ["completed", "cancelled", "failed"]) {
    const html = workspace({ team: team(state), teamLeadActive: false });
    assert.doesNotMatch(html, /aria-label="输入框团队状态"/);
    assert.match(html, /aria-label="发送"/);
  }
  const solo = workspace({ team: undefined, teamLeadActive: false, run: buildRunHistory("solo", "running", []) });
  assert.match(solo, /aria-label="停止生成"/);
  assert.match(solo.match(/<textarea[^>]*>/)[0], /\sdisabled(?:=|[ >])/);
});

test("team coordination is not presented as a member working and recovery feedback stays by the composer", () => {
  const idle = team(); idle.sessions = [];
  const html = render(h(ComposerTeamStatus, { team: idle, onPause: noop }));
  assert.match(html, /等待调度、审查或集成/);
  assert.doesNotMatch(html, /位成员工作中/);
  const recovery = render(h(ComposerTeamStatus, { team: team("paused"), onResume: noop, busy: true, error: "工作目录缺失" }));
  assert.match(recovery, /正在检查工作现场/);
  assert.match(recovery, /工作目录缺失/);
  assert.match(recovery.match(/<button[^>]*>/)[0], /\sdisabled(?:=|[ >])/);
});

test("restart recovery replaces the misleading running spinner and placeholder", () => {
  const recovery = team();
  recovery.sessions = [{ agent_id: "a", generation: 1, state: "work" }, { agent_id: "a", generation: 2, state: "idle" }];
  recovery.recoveries = [{ attempt_id: "one", result_unknown: true }, { attempt_id: "two", result_unknown: true }];
  const html = workspace({ team: recovery });
  assert.match(html, /团队等待恢复/);
  assert.match(html, /2 项任务需检查/);
  assert.doesNotMatch(html, /团队正在执行|团队运行中|等待调度、审查或集成/);
  const strip = render(h(ComposerTeamStatus, { team: recovery }));
  assert.doesNotMatch(strip, /animate-spin/);
  recovery.sessions.push({ agent_id: "b", generation: 1, state: "work" });
  assert.match(render(h(ComposerTeamStatus, { team: recovery })), /团队部分任务等待恢复/);
  assert.match(render(h(ComposerTeamStatus, { team: recovery })), /1 位成员工作中/);
});
