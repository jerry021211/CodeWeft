import { CircleAlert, Pause, Play, Users } from "lucide-react";
import type { TeamSnapshot } from "@/types/api";
import { isTeamActive, teamStatusLabel, teamExecutionState } from "@/lib/teamPresentation";
import { Spinner } from "@/components/ui";

/** Team execution outlives the planning turn; do not use the root Run's status. */
export function ComposerTeamStatus({ team, busy, error, onPause, onResume }: {
  team?: TeamSnapshot; busy?: boolean; error?: string; onPause?: () => void; onResume?: () => void;
}) {
  if (!team || (!isTeamActive(team) && team.team.state !== "paused")) return null;
  const state = team.team.state;
  const { working, manual, waitingRecovery } = teamExecutionState(team);
  const running = state === "running";
  const pausing = state === "pausing";
  const paused = state === "paused";
  const actionClass = "inline-flex shrink-0 items-center gap-1.5 rounded-full border border-line bg-surface px-3 py-1.5 text-xs font-medium text-ink transition hover:bg-surface-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-50";
  return <div className="mx-3 mt-3 rounded-xl bg-surface-muted px-3 py-2.5 sm:mx-4" aria-label="输入框团队状态">
    <div className="flex flex-wrap items-center gap-2.5">
      <div className="flex min-w-0 flex-1 items-center gap-2" role="status" aria-live="polite">
        {waitingRecovery ? <CircleAlert className="size-3.5 shrink-0 text-warning" /> : running || pausing ? <Spinner className="size-3.5 shrink-0 text-accent" /> : <Users className="size-3.5 shrink-0 text-ink-muted" />}
        <span className="text-xs font-medium text-ink">{teamStatusLabel(team)}
          {running && <span className="ml-1.5 font-normal text-ink-muted">· {waitingRecovery ? `${manual} 项任务需检查，请查看团队恢复事项` : working > 0 ? `${working} 位成员工作中` : "等待调度、审查或集成"}</span>}
        </span>
      </div>
      {running && onPause && <button type="button" className={actionClass} disabled={busy} onClick={onPause}>
        {busy ? <Spinner className="size-3" /> : <Pause className="size-3" />}{busy ? "正在处理…" : "暂停团队"}
      </button>}
      {pausing && <span className="text-xs text-ink-muted">等待当前操作退出…</span>}
      {paused && onResume && <button type="button" className={actionClass} disabled={busy} onClick={onResume}>
        {busy ? <Spinner className="size-3" /> : <Play className="size-3" />}{busy ? "正在检查工作现场…" : "继续执行"}
      </button>}
    </div>
    {error && <p role="alert" className="mt-2 break-words text-xs text-danger">操作未完成：{error}</p>}
  </div>;
}
