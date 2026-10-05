import { Bot, ChevronRight } from "lucide-react";
import type { TeamSession, TeamSnapshot } from "@/types/api";
import { isTeamActive, sessionProgress, teamSessionGroups, waitingReasonLabel } from "@/lib/teamPresentation";
import { formatTime, prettyJson } from "@/lib/utils";
import { StatusDot } from "@/components/ui";

export function TeamSessions({ team }: { team: TeamSnapshot }) {
  const groups = teamSessionGroups(team);
  const names = new Map(team.agents.map(agent => [agent.id, agent.name]));
  const history = groups.flatMap(group => group.history);
  return <section aria-label="团队成员">
    <div className="mb-2.5 flex items-center gap-2 text-[10px] font-bold text-ink-muted">
      <Bot className="size-3.5" /><span>团队成员</span>
      <span className="ml-auto rounded-full bg-surface-strong px-1.5 py-0.5 font-mono text-[8px] text-ink-faint">{groups.length}</span>
    </div>
    <div className="space-y-1.5">
      {groups.map(({ agentId, current, history: previous }) => <SessionCard key={agentId} session={current}
        name={names.get(agentId) ?? agentId} active={isTeamActive(team)} previous={previous.length} />)}
    </div>
    {history.length > 0 && <details key={team.team.id} className="group/history mt-3 rounded-xl border border-line">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-3 py-2.5 text-[10px] text-ink-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-3.5 transition-transform group-open/history:rotate-90" />
        历史运行记录<span className="ml-auto text-ink-faint">{history.length} 条</span>
      </summary>
      <div className="space-y-1.5 border-t border-line px-2 pb-2 pt-2">
        <p className="px-1 pb-1 text-[9px] leading-4 text-ink-faint">以下是同一成员的旧会话，不代表新增成员或当前仍在运行。</p>
        {history.map(session => <SessionCard key={session.id} session={session} name={names.get(session.agent_id) ?? session.agent_id} active={false} historical />)}
      </div>
    </details>}
  </section>;
}

const states: Record<string, string> = { starting: "启动中", work: "工作中", idle: "待命", waiting: "等待协调", suspect: "连接待确认", lost: "执行中断", failed: "执行失败", shutdown: "已停止" };
function SessionCard({ session, name, active, historical = false, previous = 0 }: {
  session: TeamSession; name: string; active: boolean; historical?: boolean; previous?: number;
}) {
  const progress = sessionProgress(session, active);
  const working = active && session.state === "work";
  const status = historical ? "idle" : ["failed", "lost"].includes(session.state) ? "error"
    : working ? "running" : ["waiting", "suspect"].includes(session.state) ? "warning" : "idle";
  return <div className="rounded-xl border border-line px-3 py-2.5">
    <div className="flex items-center gap-2 text-[10px]">
      <StatusDot status={status} pulse={working && !historical} />
      <span className="min-w-0 flex-1 truncate font-medium text-ink">{name}</span>
      <span className="shrink-0 text-[9px] text-ink-faint" title={`同一成员的第 ${session.generation} 代运行会话`}>第 {session.generation} 次会话</span>
    </div>
    <div className="mt-1 text-[9px] text-ink-muted">{historical ? "历史状态：" : ""}{!historical && !active && session.state === "work" ? "已暂停或结束" : states[session.state] ?? session.state} · {historical ? "最后活动" : "最近活动"} {formatTime(session.heartbeat_at)}</div>
    {!historical && previous > 0 && <div className="mt-1 text-[9px] text-ink-faint">{previous} 条旧会话已归入历史记录</div>}
    {progress.phase && <div className="mt-1 text-[9px] text-ink-muted">{progress.phase}</div>}
    {progress.reason && <div className={`mt-1 text-[9px] ${historical ? "text-ink-muted" : "text-warning"}`}>{waitingReasonLabel(progress.reason)}</div>}
    {session.failure && <div className="mt-1 whitespace-pre-wrap break-words text-[9px] text-ink-muted">{typeof session.failure.message === "string" ? session.failure.message : prettyJson(session.failure)}</div>}
  </div>;
}
