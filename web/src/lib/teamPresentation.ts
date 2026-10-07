import type { RunEvent, RunStatus, TeamSession, TeamSnapshot } from "@/types/api";
import { buildRunHistory } from "@/store/runStore";
import { buildProcessPresentation } from "@/lib/processPresentation";

const terminal = new Set(["completed", "failed", "cancelled", "closed_with_unmerged_candidates"]);
const phaseLabels: Record<string, string> = {
  model_receiving: "正在接收模型回复", model_waiting: "等待模型响应（思考期间可能没有正文）",
  tool_executing: "正在执行工具", model_retry_wait: "等待模型重试",
  executing: "正在处理本轮结果", permission_waiting: "等待工具权限审批", user_input_waiting: "等待用户回答",
};
export function sessionProgress(session?: TeamSession, active = true) {
  // Accept old server snapshots while keeping normal progress out of warnings.
  const legacyPhase = session?.waiting_reason && Object.hasOwn(phaseLabels, session.waiting_reason) ? session.waiting_reason : undefined;
  const phase = session?.activity_phase ?? legacyPhase;
  const reason = session?.waiting_reason && !legacyPhase && ["waiting", "suspect", "lost", "failed"].includes(session.state)
    ? session.waiting_reason : undefined;
  return { phase: active && session?.state === "work" && phase ? phaseLabels[phase] ?? phase : undefined, reason };
}
export function waitingReasonLabel(reason: string) {
  if (reason.startsWith("waiting_for_lead_answer:")) return "等待 Lead 回答指定问题";
  return ({ team_plan_change_required: "需要调整批准方案，等待用户指示",
    candidate_review: "等待 Lead 审查成果", runtime_validation: "等待成果验证",
    model_response_timeout: "模型长时间没有有效响应，已请求停止",
    model_call_timeout: "本轮模型调用达到总时限，已请求停止",
    worker_heartbeat_timeout: "执行进度异常，正在确认成员是否停止",
    unknown_write_result: "写操作结果不确定，需要检查工作现场", scope_violation: "修改超出任务范围，需要检查工作现场",
    service_restart: "服务重启中断了执行，需要检查并恢复", team_paused: "团队已暂停",
  } as Record<string, string>)[reason] ?? reason;
}
export function isTeamActive(team: TeamSnapshot) { return !terminal.has(team.team.state) && team.team.state !== "paused"; }
export function teamSessionGroups(team?: Pick<TeamSnapshot, "agents" | "sessions">) {
  const grouped = new Map<string, TeamSession[]>();
  // Keep the member order stable when a new generation appears.
  for (const agent of team?.agents ?? []) grouped.set(agent.id, []);
  for (const session of team?.sessions ?? []) {
    const sessions = grouped.get(session.agent_id) ?? [];
    sessions.push(session);
    grouped.set(session.agent_id, sessions);
  }
  return [...grouped].flatMap(([agentId, sessions]) => {
    const [current, ...history] = sessions.sort((a, b) => b.generation - a.generation);
    return current ? [{ agentId, current, history }] : [];
  });
}
export function teamExecutionState(team?: TeamSnapshot) {
  const latest = new Map<string, TeamSession>();
  for (const session of team?.sessions ?? []) {
    if ((latest.get(session.agent_id)?.generation ?? -1) < session.generation) latest.set(session.agent_id, session);
  }
  const working = [...latest.values()].filter(session => ["work", "starting"].includes(session.state)).length;
  const recoveries = team?.recoveries ?? [];
  const manual = recoveries.filter(item => !item.automatic).length;
  const automatic = recoveries.length - manual;
  const integrationBlocked = (team?.integrations ?? []).filter(op => ["validation_failed", "conflicted", "interrupted", "recovery_required"].includes(op.status)).length;
  return { working, manual, automatic, integrationBlocked,
    waitingIntegration: team?.team.state === "running" && integrationBlocked > 0 && working === 0,
    waitingRecovery: team?.team.state === "running" && manual > 0 && working === 0 };
}
export function teamStatusLabel(team: TeamSnapshot) {
  const progress = teamExecutionState(team);
  if (team.team.state === "running") {
    if (progress.waitingRecovery) return "团队等待恢复";
    if (progress.waitingIntegration) return "团队等待集成问题处理";
    if (progress.manual) return "团队部分任务等待恢复";
    if (progress.automatic && !progress.working) return "团队正在检查恢复现场";
  }
  return ({ planning: "团队正在规划", waiting_approval: "团队等待方案确认", running: "团队正在执行",
    ready_for_manual_integration: "团队等待集成", completed: "团队已完成", failed: "团队执行失败",
    pausing: "团队正在暂停", paused: "团队已暂停", cancelled: "团队已停止", closed_with_unmerged_candidates: "团队已结束，成果尚未全部集成" } as Record<string, string>)[team.team.state] ?? team.team.state;
}
const memberStates: Record<string, string> = { starting: "正在启动", work: "工作中", idle: "待命", waiting: "等待协调",
  suspect: "连接待确认", lost: "连接丢失", failed: "执行失败", shutdown: "已停止" };
const attemptStates: Record<string, string> = { assigned: "已领取", plan_required: "准备执行计划", plan_submitted: "等待计划审查",
  plan_approved: "准备实现", running: "实现中", waiting: "等待协调", candidate_submitted: "等待成果审查",
  review_rejected: "需要返工", validating: "验证中", validation_failed: "验证失败", committing: "保存成果",
  succeeded: "已交付", failed: "失败", cancelled: "已取消", orphaned: "等待恢复" };

export function teamMembers(team: TeamSnapshot, events: RunEvent[]) {
  const active = isTeamActive(team);
  return team.agents.map(agent => {
    const session = team.sessions.filter(item => item.agent_id === agent.id).sort((a, b) => b.generation - a.generation)[0];
    const attempt = team.attempts.find(item => item.id === session?.current_attempt_id)
      ?? team.attempts.filter(item => item.agent_id === agent.id).at(-1);
    const task = team.tasks.find(item => item.task.id === attempt?.task_id)?.task;
    const working = active && (session?.state === "work" || session?.state === "starting");
    const failed = session?.state === "failed" || session?.state === "lost" || Boolean(session?.failure);
    const waiting = active && (session?.state === "waiting" || session?.state === "suspect");
    const status: RunStatus = working ? "running" : failed ? "failed" : waiting ? "waiting_approval" : "completed";
    const ownEvents = events.filter(event => event.agent_id === agent.id);
    const presentation = buildProcessPresentation(buildRunHistory(team.team.root_run_id, status, ownEvents));
    const latest = presentation.entries.at(-1);
    const candidate = team.candidates.filter(item => item.attempt_id === attempt?.id).at(-1);
    return { agent, session, attempt, task, working, failed, waiting, presentation, progress: sessionProgress(session, active),
      label: !active ? (team.team.state === "paused" ? "已暂停" : team.team.state === "cancelled" ? "已停止" : failed ? "执行失败" : "已结束")
        : memberStates[session?.state ?? "starting"] ?? session?.state,
      taskStatus: attempt ? `${!active && !["succeeded", "failed", "cancelled"].includes(attempt.state) ? team.team.state === "paused" ? "暂停前：" : "结束前：" : ""}${attemptStates[attempt.state] ?? attempt.state}` : undefined,
      activity: latest ? `${latest.status === "running" ? "正在" : "最近"}：${[latest.label, latest.target].filter(Boolean).join(" · ")}`
        : team.team.state === "paused" ? "工作现场已保留，等待继续执行" : working ? "正在处理任务，等待首条工作记录" : agent.role === "lead" ? "协调任务、审查成果" : "等待可执行任务",
      summary: candidate?.summary,
    };
  });
}
