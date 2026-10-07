import { useState } from "react";
import { CircleAlert } from "lucide-react";
import type { TeamRecovery } from "@/types/api";

export function RecoveryCard({ recovery, taskName, teammateName, busy, onResume }: { recovery: TeamRecovery; taskName?: string; teammateName?: string; busy?: boolean; onResume: (attemptId: string, reason: string, acknowledgeUnknownResult: boolean) => void }) {
  const [reason, setReason] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  if (recovery.automatic) return <div className="rounded-xl border border-line p-3 text-xs text-ink-muted" role="status">
    Task #{recovery.task_id} · 正在自动检查恢复现场；若团队已暂停，将在继续执行后检查。
  </div>;
  const ready = recovery.recoverable && (!recovery.result_unknown || (acknowledged && reason.trim()));
  return (
    <div className="rounded-xl border border-warning/30 bg-warning/5 p-3 text-xs">
      <div className="flex items-start gap-2">
        <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" />
        <div className="min-w-0 flex-1">
          <div className="text-xs font-semibold text-ink">Task #{recovery.task_id}{taskName ? ` · ${taskName}` : ""}</div>
          <div className="mt-1 text-ink-faint">Teammate：{teammateName || recovery.agent_id}</div>
          <div className="mt-1 leading-5 text-ink-muted">{recovery.summary}</div>
        </div>
      </div>
      <div className="mt-2 space-y-1 leading-5 text-ink-muted">
        {recovery.tool_name && <div>相关操作：{recovery.tool_name === "team_submit_candidate" ? "提交代码成果" : recovery.tool_name} · {recovery.result_unknown ? "执行结果待核实" : recovery.tool_status === "failed" ? "失败" : recovery.tool_status === "scope_violation" ? "超出允许范围" : "需要检查"}</div>}
        {recovery.tool_error && <details className="break-words"><summary className="cursor-pointer">查看失败原因</summary><div className="mt-1 whitespace-pre-wrap">{recovery.tool_error}</div></details>}
        {recovery.allowed_scopes.length > 0 && <div>允许范围：{recovery.allowed_scopes.join(", ")}</div>}
        {recovery.outside_paths.length > 0 && <div className="text-warning">越界文件：{recovery.outside_paths.join(", ")}</div>}
        {recovery.worktree_path && <div className="break-all font-mono text-ink-faint">Worktree：{recovery.worktree_path}</div>}
        {recovery.blocking_checks.length > 0 && <div>待确认：{recovery.blocking_checks.map(recoveryCheckLabel).join("；")}</div>}
        <div>继续前会检查方案授权、工作目录和实际修改；通过后，成员从保留的进度继续，提交仍需通过范围检查。</div>
      </div>
      <textarea value={reason} onChange={(event) => setReason(event.target.value)} placeholder={recovery.result_unknown ? "填写现场核验说明（必填）" : "补充处理说明（选填）"} className="mt-2 min-h-14 w-full resize-y rounded-lg border border-line bg-surface px-2.5 py-2 text-xs text-ink outline-none focus:border-accent" />
      {recovery.result_unknown && (
        <label className="mt-2 flex items-start gap-2 leading-5 text-warning">
          <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} className="mt-0.5" />
          <span>我已检查Worktree现状，并理解Runtime不会自动重放上一次写操作。</span>
        </label>
      )}
      {!recovery.recoverable && <div className="mt-2 text-danger">该遗留记录当前不能直接恢复，请保留现场并取消或等待兼容恢复。</div>}
      <button type="button" disabled={busy || !ready} onClick={() => onResume(recovery.attempt_id, reason.trim() || "用户请求重新核验已保留的工作现场并继续", acknowledged)} className="mt-2 rounded-lg bg-accent px-2.5 py-1.5 text-xs font-semibold text-white disabled:opacity-40">重新检查并继续</button>
    </div>
  );
}

function recoveryCheckLabel(value: string): string {
  const labels: Record<string, string> = {
    unknown_result_acknowledgement_required: "确认未知写结果",
    runtime_recheck_required: "等待Runtime重新校验现场",
  };
  return labels[value] ?? value;
}
