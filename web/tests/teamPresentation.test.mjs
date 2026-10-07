import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-team-presentation-"));
const bundle = join(directory, "team.cjs");
await build({ stdin: { contents: `export * from './src/lib/teamPresentation'; export { api } from './src/lib/api';
export { TeamConversation } from './src/components/TeamConversation';
export { TeamPanel } from './src/components/TeamPanel';
export { TeamDeliveryProgress } from './src/components/TeamDeliveryProgress';
export { ComposerTeamStatus } from './src/components/ComposerTeamStatus';
export { createElement } from 'react'; export { renderToStaticMarkup } from 'react-dom/server';`, resolveDir: resolve(".") },
  tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle });
const { teamMembers, isTeamActive, teamStatusLabel, sessionProgress, api, TeamConversation, TeamPanel, TeamDeliveryProgress, ComposerTeamStatus, createElement, renderToStaticMarkup } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });

function snapshot(state = "running") {
  return { team: { id: "team", state, root_run_id: "planning-run" },
    agents: [{ id: "a", name: "后端成员", role: "teammate" }, { id: "b", name: "前端成员", role: "teammate" }],
    sessions: [{ agent_id: "a", state: "work", generation: 1, current_attempt_id: "aa" }, { agent_id: "b", state: "waiting", generation: 1, current_attempt_id: "bb", waiting_reason: "waiting_for_lead_answer:q1" }],
    attempts: [{ id: "aa", agent_id: "a", task_id: "1", state: "running" }, { id: "bb", agent_id: "b", task_id: "2", state: "waiting" }],
    tasks: [{ task: { id: "1", subject: "实现订单事务" } }, { task: { id: "2", subject: "开发选座图" } }], candidates: [],
  };
}
function event(seq, agent, type, payload) {
  return { id: `e${seq}`, seq, agent_id: agent, parent_agent_id: "lead", run_id: "planning-run", type,
    occurred_at: "2026-10-05T00:00:00Z", payload };
}

test("failed candidate recovery exposes the failure and offending files without claiming unknown execution", () => {
  const team = { team: { id: "team", state: "running", base_commit: "base" }, usage: {},
    agents: [], sessions: [], attempts: [], tasks: [], candidates: [], plans: [],
    attempt_plans: [], worktrees: [], validation_runs: [], scheduling: [], messages: [],
    manual_integration: { commands: [] },
    recoveries: [{ attempt_id: "attempt", task_id: "1", agent_id: "a", reason_code: "protocol_incomplete",
      summary: "代码成果提交失败，任务已暂停", recoverable: true, result_unknown: false,
      tool_name: "team_submit_candidate", tool_status: "failed", tool_error: "Candidate outside approved scope",
      tool_executed: false, allowed_scopes: ["server/**"], outside_paths: ["web/app.ts"], blocking_checks: [] }],
  };
  const html = renderToStaticMarkup(createElement(TeamPanel, { enabled: true, team }));
  assert.match(html, /提交代码成果/);
  assert.match(html, /查看失败原因/);
  assert.match(html, /web\/app.ts/);
  assert.match(html, /Candidate outside approved scope/);
  assert.doesNotMatch(html, /未执行或未确认执行|执行结果待核实|我已检查Worktree现状/);
});

test("running Team does not inherit its completed planning run status", () => {
  const team = snapshot();
  assert.equal(isTeamActive(team), true);
  assert.equal(teamStatusLabel(team), "团队正在执行");
  const members = teamMembers(team, [event(1, "root", "run.completed", {})]);
  assert.equal(members[0].working, true);
  assert.equal(members[0].task.subject, "实现订单事务");
  assert.equal(members[1].working, false);
  assert.equal(members[1].waiting, true);
});

test("normal progress, including legacy snapshots, is never a recovery warning", () => {
  for (const legacy of [false, true]) {
    const team = snapshot();
    team.sessions = [team.sessions[0]];
    team.agents = [team.agents[0]];
    team.tasks.forEach(({ task }) => { task.blockedBy = []; task.status = "in_progress"; });
    team.recoveries = []; team.scheduling = [];
    team.sessions[0][legacy ? "waiting_reason" : "activity_phase"] = "model_receiving";
    const html = renderToStaticMarkup(createElement(TeamConversation, { team }));
    assert.match(html, /正在接收模型回复/);
    assert.doesNotMatch(html, /等待协调或恢复|等待原因|model_receiving/);
    assert.equal(teamMembers(team, [])[0].working, true);
    assert.equal(sessionProgress(team.sessions[0]).reason, undefined);
  }
});

test("real waits retain their reason and stale progress is hidden after exit", () => {
  for (const reason of ["model_response_timeout", "waiting_for_lead_answer:q1", "unknown_write_result"]) {
    const progress = sessionProgress({ state: "waiting", activity_phase: "model_receiving", waiting_reason: reason });
    assert.equal(progress.reason, reason);
    assert.equal(progress.phase, undefined);
  }
  assert.equal(sessionProgress({ state: "idle", activity_phase: "tool_executing" }).phase, undefined);
  assert.equal(sessionProgress({ state: "work", activity_phase: "model_receiving" }, false).phase, undefined);
});

test("members with identical tool call IDs keep their own work and outputs", () => {
  const events = [event(1, "a", "tool.started", { tool_use_id: "call", name: "write_file", input: { file_path: "server/orders.py" } }),
    event(2, "b", "tool.started", { tool_use_id: "call", name: "read_file", input: { file_path: "web/Seats.tsx" } }),
    event(3, "b", "tool.completed", { tool_use_id: "call", name: "read_file", output: "seat map source" })];
  const members = teamMembers(snapshot(), events);
  assert.equal(members[0].presentation.entries[0].status, "running");
  assert.match(members[0].activity, /server\/orders.py/);
  assert.doesNotMatch(members[0].activity, /Seats/);
  assert.match(members[1].activity, /web\/Seats.tsx/);
  assert.equal(members[1].presentation.summary.toolCount, 1);
});

test("latest session generation wins and terminal Teams never show working spinners", () => {
  const team = snapshot();
  team.sessions.push({ agent_id: "a", generation: 2, state: "idle" });
  assert.equal(teamMembers(team, [])[0].label, "待命");
  for (const state of ["completed", "failed", "cancelled", "closed_with_unmerged_candidates"]) {
    const terminal = snapshot(state);
    assert.equal(isTeamActive(terminal), false);
    assert.equal(teamMembers(terminal, []).some(member => member.working || member.waiting), false);
    assert.match(teamMembers(terminal, [])[0].taskStatus, /^结束前：/);
  }
});

test("task delivery and candidate summary do not falsely mark the Team complete", () => {
  const team = snapshot();
  team.attempts[0].state = "succeeded";
  team.candidates.push({ attempt_id: "aa", summary: "订单测试通过，等待集成" });
  const member = teamMembers(team, [])[0];
  assert.equal(member.taskStatus, "已交付");
  assert.equal(member.summary, "订单测试通过，等待集成");
  assert.equal(teamStatusLabel(team), "团队正在执行");
});

test("paused teams have no member spinner and retain resumable task details", () => {
  const team = snapshot("paused");
  assert.equal(isTeamActive(team), false);
  assert.equal(teamStatusLabel(team), "团队已暂停");
  assert.equal(teamMembers(team, [])[0].label, "已暂停");
  assert.equal(teamMembers(team, [])[0].working, false);
  assert.equal(teamMembers(team, [])[0].taskStatus, "暂停前：实现中");
  assert.equal(isTeamActive(snapshot("pausing")), true);
  assert.equal(teamStatusLabel(snapshot("pausing")), "团队正在暂停");
});

test("pause and resume use dedicated endpoints instead of terminal cancellation", async () => {
  const original = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return new Response(JSON.stringify(snapshot("paused")), { headers: { "content-type": "application/json" } });
  };
  try {
    await api.controlTeam("team", "pause", "保留现场");
    await api.controlTeam("team", "resume", "继续执行");
    assert.match(requests[0].url, /teams\/team\/pause$/);
    assert.match(requests[1].url, /teams\/team\/resume$/);
    assert.ok(requests[0].body.commandId);
    assert.notEqual(requests[0].body.commandId, requests[1].body.commandId);
  } finally { globalThis.fetch = original; }
});

test("incremental activity reads use their cursor even when the parent run completed", async () => {
  const original = globalThis.fetch;
  const signal = new AbortController().signal;
  const urls = [];
  globalThis.fetch = async (url, options) => {
    urls.push(url);
    assert.equal(options.signal, signal);
    return new Response(JSON.stringify({ run_id: "planning-run", status: "completed", events: [event(51, "a", "tool.started", { name: "bash" })], next_after: null }), { headers: { "content-type": "application/json" } });
  };
  try {
    const page = await api.getRunActivityPage("planning-run", 50, signal);
    assert.equal(page.events[0].seq, 51);
    assert.equal(page.status, "completed");
    assert.match(urls[0], /after=50$/);
  } finally { globalThis.fetch = original; }
});


test("main conversation exposes task-specific failure logs and keeps long reports collapsed", () => {
  const team = snapshot();
  team.recoveries = []; team.scheduling = [];
  team.candidates = [{ id: "candidate", task_id: "1", status: "committed", summary: "实现说明", changed_files: ["core.py"], untracked_files: ["core.py", "cli.py"], known_risks: ["外部连接尚未验证"] }];
  team.integrations = [{ id: "operation", kind: "candidate", candidate_id: "candidate", status: "validation_failed",
    result: { diagnostic: { category: "environment", summary: "验证命令无法启动", owner: "lead" } },
    validations: [{ command: "python --version", status: "failed", exit_code: 1, output_excerpt: "FileNotFoundError: python" }] }];
  const html = renderToStaticMarkup(createElement(TeamConversation, { team, onResolveIntegration: () => {} }));
  assert.match(html, /任务 #1 · 实现订单事务/);
  assert.match(html, /验证命令无法启动/);
  assert.match(html, /FileNotFoundError: python/);
  assert.match(html, /2 个文件/);
  assert.match(html, /环境处理后重新验证/);
  assert.doesNotMatch(html, /交给 Lead 安排修复/);
  assert.match(html, /<details[^>]*><summary[^>]*>成果说明与审查记录/);
  assert.doesNotMatch(html, /<details[^>]*open/);
});

test("composer labels blocked integration instead of presenting idle workers as running", () => {
  const team = snapshot(); team.sessions = [];
  team.integrations = [{ id: "failed", status: "validation_failed" }];
  assert.equal(teamStatusLabel(team), "团队等待集成问题处理");
  const html = renderToStaticMarkup(createElement(ComposerTeamStatus, { team }));
  assert.match(html, /1 项检查需处理/);
  assert.doesNotMatch(html, /等待调度、审查或集成/);
  team.integrations[0].status = "superseded";
  assert.equal(teamStatusLabel(team), "团队正在执行");
});

test("known recovery failures can be rechecked directly in the conversation", () => {
  const team = snapshot(); team.scheduling = [];
  team.recoveries = [{ attempt_id: "aa", task_id: "1", agent_id: "a", recoverable: true,
    result_unknown: false, summary: "提交失败，需重新核验", allowed_scopes: ["**"], outside_paths: [], blocking_checks: [] }];
  const html = renderToStaticMarkup(createElement(TeamConversation, { team, onResumeAttempt: () => {} }));
  assert.match(html, /补充处理说明（选填）/);
  assert.match(html, /重新检查并继续/);
  assert.doesNotMatch(html, /填写现场核验说明（必填）/);
});
