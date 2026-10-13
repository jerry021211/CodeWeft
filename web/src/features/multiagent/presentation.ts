import type { RunViewState } from "@/store/runStore";
import { asRecord, asString, isRunActive } from "@/lib/utils";

export type CollaborationSnapshot = Record<string, unknown>;
export type CollaborationSync = {snapshots?: CollaborationSnapshot[]; error: boolean; syncing: boolean; retry: () => void};
export const collaborationLabels: Record<string, string> = {
  pending: "等待调度", running: "执行中", reported: "等待验证", verifying: "验证成果", delivering: "回写本地",
  accepted: "阶段已完成", completed: "已完成", paused: "已暂停", blocked: "需要处理", failed: "执行失败", closed: "已结束", unconfirmed: "等待核对状态",
};
export const memberRoles: Record<string, string> = {research: "调查", implement: "实现", review: "审查", verify: "验证"};

export function nativeSnapshots(run: RunViewState) {
  const latest = new Map<string, CollaborationSnapshot>();
  for (const event of run.events) {
    if (event.type !== "collaboration.snapshot") continue;
    const id = asString(event.payload.id);
    if (id && Number(event.payload.revision ?? 0) >= Number(latest.get(id)?.revision ?? -1)) latest.set(id, event.payload);
  }
  return [...latest.values()];
}

export function collaborationTasks(snapshot: CollaborationSnapshot) {
  return Array.isArray(snapshot.tasks) ? snapshot.tasks.map(asRecord).filter(task => asString(task.id)) : [];
}

export function memberId(snapshot: CollaborationSnapshot, taskId: string) {
  return asString(asRecord(asRecord(snapshot.records)[taskId]).member_id) || `${snapshot.id}_${taskId}`;
}

export function memberPhase(run: RunViewState, snapshot: CollaborationSnapshot, taskId: string) {
  const phase = asString(asRecord(asRecord(snapshot.records)[taskId]).status) || "pending";
  if (["accepted", "completed", "failed", "blocked", "closed"].includes(phase)) return phase;
  if (snapshot.status === "paused" || phase === "paused") return "paused";
  if (["blocked", "closed"].includes(String(snapshot.status))) return String(snapshot.status);
  if (!isRunActive(run.status) || run.status === "cancelling") return "unconfirmed";
  return phase;
}

/** Project a single actor before grouping tools. Identical tool ids in other members must never leak. */
export function memberTrace(run: RunViewState, snapshot: CollaborationSnapshot, taskId: string): RunViewState {
  const actor = memberId(snapshot, taskId);
  const actions = Object.fromEntries(Object.entries(run.actions ?? {}).filter(([, action]) => action.agent_id === actor));
  const phase = memberPhase(run, snapshot, taskId);
  const status = ["accepted", "completed"].includes(phase) ? "completed" : ["failed", "blocked"].includes(phase) ? "failed"
    : ["paused", "closed", "unconfirmed"].includes(phase) ? "cancelled" : run.status;
  return {...run, status, events: run.events.filter(event => event.agent_id === actor), actions,
    actionOrder: (run.actionOrder ?? []).filter(id => id in actions), agents: run.agents ?? {}};
}

/** Use execution revision across runs and HTTP reconciliation; never infer completion from Task counts. */
export function resolveCollaboration(run: RunViewState, id: string, relatedRuns: Record<string, RunViewState> = {}, snapshots: CollaborationSnapshot[] = []) {
  let owner = run;
  let latest = nativeSnapshots(run).find(value => value.id === id)!;
  for (const item of Object.values({...relatedRuns, [run.runId]: run})) {
    for (const value of nativeSnapshots(item)) {
      if (value.id === id && Number(value.revision) >= Number(latest?.revision ?? -1)) {latest = value; owner = item;}
    }
  }
  for (const value of snapshots) {
    if (value.id === id && Number(value.revision) >= Number(latest?.revision ?? -1)) latest = value;
  }
  return {run: owner, snapshot: latest};
}

/** Native member traces live in the side panel; ordinary Agent and old subagent views stay unchanged. */
export function rootProcessRun(run: RunViewState): RunViewState {
  const actors = new Set(nativeSnapshots(run).flatMap(snapshot => collaborationTasks(snapshot).map(task => memberId(snapshot, String(task.id)))));
  if (!actors.size) return run;
  const actions = Object.fromEntries(Object.entries(run.actions ?? {}).filter(([, action]) => !actors.has(action.agent_id ?? "")));
  return {...run, events: run.events.filter(event => !actors.has(event.agent_id)), actions,
    actionOrder: (run.actionOrder ?? []).filter(id => id in actions)};
}

export const GRAPH_CARD_WIDTH = 204;
export const GRAPH_CARD_HEIGHT = 126;
const COLUMN = 244, ROW = 154, PAD = 24, TOP = 52;
export type CollaborationNode = {id: string; x: number; y: number; task?: Record<string, unknown>};

/** Dependency depth, not claimed runtime waves. Iteration tolerates incomplete/corrupt historical graphs. */
export function collaborationLayout(snapshot: CollaborationSnapshot) {
  const tasks = collaborationTasks(snapshot);
  const ids = new Set(tasks.map(task => String(task.id)));
  const depths = new Map<string, number>();
  let pending = [...tasks];
  for (let step = 0; step < tasks.length && pending.length; step++) {
    const ready = pending.filter(task => dependencies(task).filter(id => ids.has(id)).every(id => depths.has(id)));
    if (!ready.length) break;
    for (const task of ready) depths.set(String(task.id), 1 + Math.max(0, ...dependencies(task).map(id => depths.get(id) ?? 0)));
    pending = pending.filter(task => !depths.has(String(task.id)));
  }
  const unresolved = pending.length > 0;
  for (const task of pending) depths.set(String(task.id), 1);
  const maxDepth = Math.max(1, ...depths.values());
  const groups = Array.from({length: maxDepth}, (_, i) => tasks.filter(task => depths.get(String(task.id)) === i + 1));
  const rows = Math.max(1, ...groups.map(group => group.length));
  const height = TOP + rows * ROW;
  const centerY = TOP + ((rows - 1) * ROW) / 2;
  const nodes: CollaborationNode[] = [{id: "$root", x: PAD, y: centerY}];
  groups.forEach((group, i) => group.forEach((task, j) => nodes.push({id: String(task.id), task,
    x: PAD + (i + 1) * COLUMN, y: TOP + ((rows - group.length) * ROW) / 2 + j * ROW})));
  nodes.push({id: "$result", x: PAD + (maxDepth + 1) * COLUMN, y: centerY});
  const byId = new Map(nodes.map(node => [node.id, node]));
  const edges: {from: string; to: string; path: string}[] = [];
  const connect = (from: string, to: string) => {
    const a = byId.get(from), b = byId.get(to);
    if (!a || !b || b.x <= a.x) return;
    const x = a.x + GRAPH_CARD_WIDTH, y = a.y + GRAPH_CARD_HEIGHT / 2, endY = b.y + GRAPH_CARD_HEIGHT / 2;
    const middle = (x + b.x) / 2;
    edges.push({from, to, path: `M ${x} ${y} C ${middle} ${y}, ${middle} ${endY}, ${b.x} ${endY}`});
  };
  const depended = new Set(tasks.flatMap(dependencies));
  for (const task of tasks) {
    const id = String(task.id), deps = dependencies(task).filter(dep => ids.has(dep));
    if (!deps.length) connect("$root", id);
    else deps.forEach(dep => connect(dep, id));
    if (!depended.has(id)) connect(id, "$result");
  }
  if (!tasks.length) connect("$root", "$result");
  return {nodes, edges, height, width: PAD * 2 + (maxDepth + 1) * COLUMN + GRAPH_CARD_WIDTH, maxDepth, unresolved};
}

export function dependencies(task: Record<string, unknown>): string[] {
  return Array.isArray(task.depends_on) ? task.depends_on.filter((value): value is string => typeof value === "string") : [];
}
