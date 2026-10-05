import { useMemo, useState } from "react";
import { Bot, ChevronDown, CircleAlert, Users } from "lucide-react";
import type { TeamSnapshot } from "@/types/api";
import { useTeamActivity } from "@/hooks/useTeamActivity";
import { isTeamActive, teamMembers, teamStatusLabel, waitingReasonLabel } from "@/lib/teamPresentation";
import { cx } from "@/lib/utils";
import { Spinner, StatusDot } from "@/components/ui";
import { ProcessStep } from "@/components/ThinkingProcess";

export function TeamConversation({ team, busy, error, onPause, onResume }: { team: TeamSnapshot; busy?: boolean; error?: string; onPause?: () => void; onResume?: () => void }) {
  const active = isTeamActive(team);
  const activity = useTeamActivity(team.team.id, team.team.root_run_id, active);
  const members = useMemo(() => teamMembers(team, activity.events), [team, activity.events]);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [earlier, setEarlier] = useState<Record<string, boolean>>({});
  const working = members.filter(member => member.working).length;
  const delivered = team.tasks.filter(({ task }) => task.status === "completed").length;
  const integrated = new Set(team.candidates.filter(candidate => candidate.integrated_at).map(candidate => candidate.task_id)).size;
  const pending = team.tasks.filter(({ task }) => task.status === "pending");
  const attention = team.recoveries.length > 0 || team.candidates.some(candidate => candidate.user_approval_required && candidate.status === "accepted" && !candidate.user_decision);
  return <section aria-label="团队执行进度" className="min-w-0 rounded-2xl border border-line bg-surface">
    <header className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-4 sm:px-5">
      <Users className="size-4 text-accent" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <h2 className="text-sm font-semibold text-ink">{teamStatusLabel(team)}</h2>
        <p className="mt-1 text-xs text-ink-muted">{active ? `${working} 位成员工作中 · ` : ""}已交付 {delivered}/{team.tasks.length} 项任务{team.team.integration_mode === "managed" ? ` · ${integrated} 项代码成果已集成` : ""}</p>
      </div>
      {team.team.state === "running" && onPause && <button type="button" disabled={busy} onClick={onPause}
        className="rounded-lg px-2.5 py-1.5 text-xs text-ink-muted hover:bg-surface-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:opacity-50">{busy ? "正在请求暂停…" : "暂停团队"}</button>}
      {["paused", "cancelled"].includes(team.team.state) && onResume && <button type="button" disabled={busy} onClick={onResume}
        className="rounded-lg bg-ink px-3 py-2 text-xs text-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:opacity-50">{busy ? "正在检查工作现场…" : team.team.state === "paused" ? "继续执行" : "检查并恢复"}</button>}
    </header>
    <div className="space-y-2 p-3 sm:p-4">
      {error && <p role="alert" className="rounded-lg bg-danger/10 px-3 py-2 text-xs text-danger">操作未完成：{error}</p>}
      {team.team.state === "pausing" && <p role="status" className="px-2 text-xs text-warning">已停止分配新任务，正在等待模型、工具和验证退出。完成后可继续执行。</p>}
      {team.team.state === "paused" && <p role="status" className="px-2 text-xs text-ink-muted">工作目录和任务进度已保留。继续执行前会检查现场，已完成任务不会重新分配。</p>}
      {activity.error && <p role="alert" className="rounded-lg bg-warning/10 px-3 py-2 text-xs text-warning">工作记录暂时无法更新，正在重连：{activity.error}</p>}
      {activity.loading && <p className="px-2 text-xs text-ink-muted">正在读取成员工作记录…</p>}
      {!members.length && <p className="px-2 py-3 text-xs text-ink-muted">{active ? "团队已启动，等待成员领取任务。" : "没有成员执行记录。"}</p>}
      {members.map(member => {
        const open = Boolean(expanded[member.agent.id]);
        const entries = member.presentation.entries;
        const visible = earlier[member.agent.id] ? entries : entries.slice(-8);
        const waiting = member.progress.reason;
        return <div key={member.agent.id} className="rounded-xl border border-line/70">
          <button type="button" aria-expanded={open} onClick={() => setExpanded(value => ({ ...value, [member.agent.id]: !open }))}
            className="flex w-full items-start gap-3 rounded-xl p-3 text-left hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40">
            <div className="mt-0.5">{member.working ? <Spinner className="size-4 text-accent" /> : member.failed ? <CircleAlert className="size-4 text-danger" /> : <Bot className="size-4 text-ink-muted" />}</div>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1"><span className="text-xs font-semibold text-ink">{member.agent.name}</span>
                <span className={cx("text-[11px]", member.working ? "text-accent" : member.failed ? "text-danger" : member.waiting ? "text-warning" : "text-ink-muted")}>{member.label}</span></div>
              <p className="mt-1 break-words text-xs text-ink">{member.task ? `${member.task.subject} · ${member.taskStatus}` : member.agent.role === "lead" ? "任务协调与成果审查" : "等待任务分配"}</p>
              <p className="mt-1 truncate text-[11px] text-ink-muted" title={member.activity}>{member.activity}</p>
              {member.progress.phase && <p className="mt-1 text-[11px] text-ink-muted">{member.progress.phase}</p>}
              {waiting && <p className="mt-1 text-[11px] text-warning">{waiting.startsWith("waiting_for_lead_answer:") ? "等待 Lead 回答" : waiting === "team_plan_change_required" ? "等待用户决定方案调整" : "等待协调或恢复，展开查看原因"}</p>}
            </div>
            <ChevronDown className={cx("mt-1 size-3.5 shrink-0 text-ink-faint transition-transform", open && "rotate-180")} aria-hidden="true" />
          </button>
          {open && <div className="border-t border-line px-3 py-4 sm:px-5">
            {waiting && <p className="mb-3 break-words text-xs text-warning">等待原因：{waitingReasonLabel(waiting)}</p>}
            {member.summary && <p className="mb-4 whitespace-pre-wrap break-words text-xs text-ink-muted">交付说明：{member.summary}</p>}
            {entries.length > 8 && <button type="button" className="mb-3 text-xs text-accent" onClick={() => setEarlier(value => ({ ...value, [member.agent.id]: !value[member.agent.id] }))}>{earlier[member.agent.id] ? "仅显示最近活动" : `展开全部 ${entries.length} 项活动`}</button>}
            <ol className="process-trace space-y-3">{visible.map(entry => <ProcessStep key={entry.id} entry={{ ...entry, agentLabel: undefined }} active={member.working || member.waiting} />)}</ol>
            {!entries.length && <p className="text-xs text-ink-muted">{activity.loading ? "正在读取执行明细…" : "暂无工具执行记录"}</p>}
          </div>}
        </div>;
      })}
      {pending.length > 0 && <details className="px-2 py-2 text-xs text-ink-muted"><summary className="cursor-pointer">待执行任务 · {pending.length}</summary>
        <ul className="mt-2 space-y-2">{pending.map(({ task }) => {
          const scheduling = team.scheduling.find(item => item.task_id === task.id);
          const dependencies = task.blockedBy.map(id => team.tasks.find(item => item.task.id === id)?.task.subject ?? id);
          return <li key={task.id}><span className="text-ink">{task.subject}</span><p className="mt-0.5 text-[11px]">{scheduling?.schedulable ? "等待空闲成员领取" : dependencies.length ? `等待依赖：${dependencies.join("、")}` : "等待调度条件满足"}</p></li>;
        })}</ul>
      </details>}
      {attention && <p role="status" className="rounded-lg bg-warning/10 px-3 py-2 text-xs text-warning">团队需要你的处理，请在右侧“团队”面板查看审批或恢复事项。</p>}
      {team.team.integration_mode === "managed" && <p className="flex items-center gap-2 px-2 pt-1 text-[11px] text-ink-faint"><StatusDot status={team.team.state === "completed" ? "success" : "idle"} />集成版本 C{team.team.integration_revision ?? 0} · 任务交付后仍需经过审查、集成与验证</p>}
    </div>
  </section>;
}
