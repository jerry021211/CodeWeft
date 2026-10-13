import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, unlinkSync, rmdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-native-ui-"));
const bundle = join(directory, "native.cjs");
await build({ stdin: { contents: `export * from './src/features/multiagent/NativeCollaboration';
export { ComposerModeMenu } from './src/components/ComposerModeMenu';
export { TaskPlan } from './src/components/TaskPlan';
export * from './src/features/multiagent/presentation';
export { useRunStore } from './src/store/runStore';
export { createElement } from 'react'; export { renderToStaticMarkup } from 'react-dom/server';`, resolveDir: resolve(".") },
tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle });
const { nativeSnapshots, NativeCollaboration, MemberExecutionPanel, TaskPlan, collaborationLayout, memberTrace, rootProcessRun, resolveCollaboration, useRunStore, createElement, renderToStaticMarkup } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });
const event = (revision, status) => ({id: `${revision}`, type: "collaboration.snapshot", payload: {
  id: "c", revision, status, objective: "检查订单", tasks: [{id: "a", objective: "验证事务", profile: "verify"}],
  records: {a: {status: "running", attempts: 1}},
}});
test("replay takes newest persisted revision, including out of order duplicates", () => {
  const values = nativeSnapshots({events: [event(2, "completed"), event(1, "running"), event(2, "completed")]});
  assert.equal(values.length, 1);
  assert.equal(values[0].status, "completed");
});
test("ended root with stale running snapshot requires reconciliation, never invents a pause", () => {
  const html = renderToStaticMarkup(createElement(NativeCollaboration, {
    run: {status: "cancelled", events: [event(1, "running")]}, onContinue() {},
  }));
  assert.match(html, /等待核对状态/);
  assert.doesNotMatch(html, /继续这次协作/);
  assert.doesNotMatch(html, /animate-spin/);
});

test("database completion repairs truncated history and stale revisions cannot roll it back", () => {
  const old = event(171, 'verifying');
  const run = {runId: 'old', status: 'completed', events: [old, {type: 'collaboration.snapshot', payload: {truncated: true, preview: 'cut off'}}]};
  const completed = {...old.payload, revision: 185, status: 'completed', records: {a: {status: 'accepted'}}};
  const sync = {snapshots: [completed], error: false, syncing: false, retry() {}};
  const html = renderToStaticMarkup(createElement(NativeCollaboration, {run, sync, onContinue() {}}));
  assert.match(html, /已完成/);
  assert.doesNotMatch(html, /已暂停|等待核对状态|继续这次协作|animate-spin/);
  assert.equal(resolveCollaboration(run, 'c', {}, [completed, old.payload]).snapshot.revision, 185);
  const newerRun = {runId: 'new', status: 'running', events: [event(190, 'running')]};
  const resolved = resolveCollaboration(run, 'c', {new: newerRun}, [completed]);
  assert.equal(resolved.snapshot.revision, 190);
  assert.equal(resolved.run.runId, 'new');
});

test("explicit pause permits continuation, failed synchronization exposes retry, delivery remains separate", () => {
  const run = {runId: 'run', status: 'completed', events: [event(5, 'paused')]};
  const paused = renderToStaticMarkup(createElement(NativeCollaboration, {run, onContinue() {}}));
  assert.match(paused, /已暂停/);
  assert.match(paused, /继续这次协作/);
  const stale = {...run, events: [event(6, 'verifying')]};
  const failed = renderToStaticMarkup(createElement(NativeCollaboration, {run: stale, sync: {error: true, syncing: false, retry() {}}, onContinue() {}}));
  assert.match(failed, /状态同步失败/);
  assert.match(failed, /重新同步/);
  assert.doesNotMatch(failed, /继续这次协作/);
  const checking = event(7, 'verifying'); checking.payload.records.a.status = 'accepted';
  const active = renderToStaticMarkup(createElement(NativeCollaboration, {run: {...run, status: 'running', events: [checking]}}));
  assert.match(active, /验证成果/);
  assert.doesNotMatch(active, /计划执行已完成/);
});
test("failure displays evidence and main Agent ownership in the conversation", () => {
  const value = event(1, "blocked");
  value.payload.issue = {owner: "main", reason: "测试命令返回 1"};
  const html = renderToStaticMarkup(createElement(NativeCollaboration, {
    run: {status: "running", events: [value]}, onContinue() {},
  }));
  assert.match(html, /主 Agent 正在处理/);
  assert.match(html, /测试命令返回 1/);
  assert.doesNotMatch(html, /继续这次协作/);
});

test("delegation uses original Task identity and does not claim managed writeback", () => {
  const value = event(1, 'completed');
  value.payload.mode = 'single';
  value.payload.tasks = [{id: '2', subject: '测试 CLI', objective: 'long requirements', profile: 'verify'}];
  value.payload.records = {'2': {status: 'accepted', attempts: 1}};
  const html = renderToStaticMarkup(createElement(NativeCollaboration, {run: {status: 'completed', events: [value]}}));
  assert.match(html, /单任务委派/);
  assert.match(html, /查看任务 #2 测试 CLI 的执行链路/);
  assert.match(html, /与任务面板共用任务/);
  assert.doesNotMatch(html, /已回写本地/);
});

test("graph positions prerequisites before dependents without inventing runtime waves", () => {
  const snapshot = {tasks: [{id: 'a'}, {id: 'b'}, {id: 'c', depends_on: ['a', 'b']}]};
  const graph = collaborationLayout(snapshot);
  const node = id => graph.nodes.find(node => node.id === id);
  assert.equal(node('a').x, node('b').x);
  assert.ok(node('c').x > node('a').x);
  assert.deepEqual(graph.edges.filter(edge => edge.to === 'c').map(edge => edge.from), ['a', 'b']);
  assert.equal(graph.unresolved, false);
  assert.equal(collaborationLayout({tasks: [{id: 'a', depends_on: ['b']}, {id: 'b', depends_on: ['a']}]}).unresolved, true);
});

test("member panel isolates colliding tool ids, retains failure evidence and keeps root trace separate", () => {
  useRunStore.setState({runs: {}});
  const snapshot = {id: 'c', status: 'blocked', tasks: [{id: '1', subject: '检查接口', objective: 'check', profile: 'verify'}, {id: '2'}],
    records: {'1': {status: 'failed', attempts: 1, member_id: 'old-member'}, '2': {status: 'running'}}};
  const events = [];
  const add = (actor, type, payload) => events.push({id: String(events.length), seq: events.length + 1, run_id: 'run',
    agent_id: actor, parent_agent_id: actor === 'root' ? undefined : 'root', conversation_id: 'conv', type, payload, occurred_at: '2026-10-13T01:00:00Z'});
  add('root', 'collaboration.snapshot', snapshot);
  add('old-member', 'tool.started', {tool_use_id: 'same', name: 'bash', input: {command: 'python check_api.py'}});
  add('old-member', 'tool.failed', {tool_use_id: 'same', name: 'bash', error: '接口校验失败'});
  add('c_2', 'tool.started', {tool_use_id: 'same', name: 'bash', input: {command: 'other_member_secret'}});
  add('root', 'tool.started', {tool_use_id: 'root', name: 'read_file', input: {file_path: 'root.md'}});
  add('root', 'collaboration.check', {id: 'c', task_id: '1', command: 'pytest', status: 'failed', output: '真实检查日志'});
  useRunStore.getState().mergeEvents(events);
  const run = useRunStore.getState().runs.run;
  const view = memberTrace(run, snapshot, '1');
  assert.equal(Object.keys(view.actions).length, 1);
  assert.ok(view.events.every(event => event.agent_id === 'old-member'));
  assert.deepEqual(rootProcessRun(run).actionOrder, ['root']);
  const html = renderToStaticMarkup(createElement(MemberExecutionPanel, {run, snapshot, taskId: '1'}));
  assert.match(html, /接口校验失败/);
  assert.match(html, /真实检查日志/);
  assert.doesNotMatch(html, /other_member_secret|root\.md|animate-spin/);
});

test("ordinary Agent trace stays untouched and queued members show an honest empty state", () => {
  const plain = {events: [], actions: {}, actionOrder: [], status: 'completed', agents: {}};
  assert.equal(rootProcessRun(plain), plain);
  const snapshot = {id: 'c', tasks: [{id: '1', subject: '等待实现', profile: 'implement'}], records: {'1': {status: 'pending'}}};
  const html = renderToStaticMarkup(createElement(MemberExecutionPanel, {run: plain, snapshot, taskId: '1'}));
  assert.match(html, /尚未开始执行/);
  assert.match(html, /不代表验证已通过/);
});

test("restored members show both runs while final delivery waits for their results", () => {
  useRunStore.setState({runs: {}});
  const snapshot = {id: 'resume', status: 'running', tasks: [{id: '1', subject: '实现库存', profile: 'implement'}], records: {'1': {status: 'running', attempts: 2}}};
  for (const [index, runId] of ['before', 'after'].entries()) {
    useRunStore.getState().mergeEvents([{id: runId, seq: 1, run_id: runId, conversation_id: 'conv', agent_id: 'resume_1',
      parent_agent_id: 'root', type: 'model.text_delta', payload: {call_id: runId, text: index ? '恢复后继续测试' : '中断前完成实现'}, occurred_at: `2026-10-13T0${index + 1}:00:00Z`}]);
  }
  const runs = Object.values(useRunStore.getState().runs), run = runs[1];
  const html = renderToStaticMarkup(createElement(MemberExecutionPanel, {run, runs, snapshot, taskId: '1'}));
  assert.match(html, /中断前完成实现/);
  assert.match(html, /恢复后继续测试/);
  const result = renderToStaticMarkup(createElement(MemberExecutionPanel, {run: {...run, status: 'running'}, snapshot, taskId: '$result'}));
  assert.match(result, /等待成员成果/);
  assert.doesNotMatch(result, /animate-spin/);
});

test("failed and cancelled canonical Tasks remain visible with recovery context", () => {
  const tasks = ['failed', 'cancelled'].map((status, index) => ({taskListId: 'list', task: {
    id: String(index + 1), subject: `task-${status}`, description: 'requirements', status,
    activeForm: status === 'failed' ? '需要修复' : '执行已结束', blockedBy: [], blocks: [], metadata: {},
  }}));
  const html = renderToStaticMarkup(createElement(TaskPlan, {tasks, onContinue() {}, onCreate() {}}));
  assert.match(html, /task-failed/);
  assert.match(html, /task-cancelled/);
  assert.match(html, /需要修复/);
  assert.match(html, /已取消/);
  assert.equal((html.match(/title="在当前会话继续处理"/g) || []).length, 1);
});
