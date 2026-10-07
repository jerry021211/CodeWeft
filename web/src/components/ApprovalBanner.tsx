import { useEffect, useState } from "react";
import { Check, ShieldAlert, X } from "lucide-react";
import type { Approval, ApprovalDecision, TeamSnapshot } from "@/types/api";
import { prettyJson } from "@/lib/utils";
import { approvalPresentation, approvalStatus } from "@/lib/approvalPresentation";
import { Spinner } from "@/components/ui";

export function ApprovalBanner({ approval, busy, error, origin, onDecision }: {
  approval: Approval; busy?: boolean; error?: string; origin?: string;
  onDecision: (decision: ApprovalDecision) => void;
}) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (approval.status !== "pending" || !approval.expires_at) return;
    const delay = Date.parse(approval.expires_at) - Date.now();
    if (!Number.isFinite(delay)) return;
    const timer = window.setTimeout(() => setNow(Date.now()), Math.min(Math.max(delay, 0) + 20, 2147483647));
    return () => window.clearTimeout(timer);
  }, [approval.status, approval.expires_at]);
  const status = approvalStatus(approval, now);
  const pending = status === "pending";
  const model = approvalPresentation(approval);
  const label = { pending: "等待你确认", allowed: "已允许本次操作", denied: "已拒绝", expired: "已失效" }[status];
  const detail = <div className="space-y-4 border-t border-line px-4 py-4 sm:px-5">
    {model.purpose && <div><p className="text-xs text-ink-faint">Agent 提供的操作说明</p><p className="mt-1 whitespace-pre-wrap break-words text-sm leading-6 text-ink">{model.purpose}</p></div>}
    <p className="text-xs leading-5 text-ink-muted">{model.explanation}</p>
    <div className="grid gap-4 sm:grid-cols-2">
      <div><h3 className="text-xs font-semibold text-ink">操作与影响提示</h3><ul className="mt-2 list-disc space-y-1.5 pl-4 text-xs leading-5 text-ink-muted">{model.effects.map(item => <li key={item}>{item}</li>)}</ul></div>
      <div><h3 className="text-xs font-semibold text-ink">需要核对的范围</h3><p className="mt-2 break-words text-xs leading-5 text-ink-muted">{model.scope}</p></div>
    </div>
    <details className="rounded-lg border border-line bg-surface-muted text-xs">
      <summary className="cursor-pointer px-3 py-2.5 font-medium text-ink-muted focus-visible:outline-accent">查看完整命令、参数与审批依据</summary>
      <div className="space-y-3 border-t border-line p-3">
        {approval.reason && <div><p className="mb-1 text-ink-faint">系统审批依据（原始记录）</p><p className="whitespace-pre-wrap break-words leading-5 text-ink-muted">{approval.reason}</p></div>}
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-code p-3 font-mono text-xs leading-5 text-code-ink">{approval.input != null ? prettyJson(approval.input) : approval.summary}</pre>
        <p className="break-all text-ink-faint">工具：{approval.tool_name} · 操作编号：{approval.id}</p>
        {approval.requested_at && <p className="text-ink-faint">请求时间：{new Date(approval.requested_at).toLocaleString()}</p>}
      </div>
    </details>
    {pending ? <>
      <p className="text-xs leading-5 text-ink-muted">暂不允许：{model.declined}</p>
      {error && <p role="alert" className="text-xs text-danger">处理未完成：{error}</p>}
      <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
        <button type="button" disabled={busy} onClick={() => onDecision("allow")} className="inline-flex items-center gap-1.5 rounded-lg bg-ink px-4 py-2.5 text-xs font-semibold text-surface hover:opacity-90 focus-visible:outline-accent disabled:opacity-50">{busy ? <Spinner className="size-3.5" /> : <Check className="size-3.5" />}允许本次操作</button>
        <button type="button" disabled={busy} onClick={() => onDecision("deny")} className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-surface px-4 py-2.5 text-xs text-ink-muted hover:bg-surface-strong focus-visible:outline-accent disabled:opacity-50"><X className="size-3.5" />暂不允许</button>
        {busy && <span role="status" className="text-xs text-ink-muted">正在提交决定…</span>}
      </div>
    </> : <p className="text-xs leading-5 text-ink-muted">{status === "allowed" ? "授权已记录。是否执行成功，请查看对应的工作记录；授权不等于操作成功。" : status === "expired" ? "该授权请求已结束，不能继续批准。若仍需执行，Agent 必须发起新的请求。" : "拒绝决定已记录，当前请求不会获准执行。"}</p>}
  </div>;
  const header = <div className="flex flex-wrap items-start gap-2.5 px-4 py-3.5 sm:px-5">
    <ShieldAlert className={`mt-0.5 size-4 shrink-0 ${pending ? "text-warning" : "text-ink-faint"}`} />
    <div className="min-w-0 flex-1"><h2 className="text-sm font-semibold text-ink">{pending ? "需要你确认" : "授权记录"} · {model.title}</h2><p className="mt-1 break-words text-xs text-ink-muted">{origin || "Agent"} · {approval.tool_name}</p></div>
    <span className={`rounded-full px-2 py-1 text-xs ${pending ? "bg-warning/10 text-warning" : "bg-surface-muted text-ink-muted"}`}>{label}</span>
  </div>;
  return pending ? <section id={`approval-${approval.id}`} aria-label="操作授权" className="overflow-hidden rounded-2xl border border-warning/30 bg-surface shadow-sm">{header}{detail}</section>
    : <details id={`approval-${approval.id}`} className="overflow-hidden rounded-xl border border-line bg-surface"><summary className="cursor-pointer list-none">{header}</summary>{detail}</details>;
}

export function ApprovalTimeline({ approvals, teams = [], busyIds = [], errors = {}, onDecision }: {
  approvals: Approval[]; teams?: TeamSnapshot[]; busyIds?: string[]; errors?: Record<string, string>;
  onDecision: (approval: Approval, decision: ApprovalDecision) => void;
}) {
  if (!approvals.length) return null;
  const pending = approvals.filter(a => a.status === "pending");
  const history = approvals.filter(a => a.status !== "pending");
  const render = (approval: Approval) => {
    const team = teams.find(t => t.team.id === approval.team_run_id);
    const attempt = team?.attempts.find(a => a.id === approval.attempt_id);
    const task = team?.tasks.find(t => t.task.id === attempt?.task_id)?.task;
    const member = team?.agents.find(a => a.id === (approval.agent_id || attempt?.agent_id));
    const origin = [member?.name || (approval.agent_id ? `成员 ${approval.agent_id}` : "Agent"), task ? `任务 #${task.id} · ${task.subject}` : attempt ? `任务 #${attempt.task_id}` : `运行 ${approval.run_id.slice(-8)}`].join(" · ");
    return <ApprovalBanner key={approval.id} approval={approval} origin={origin} busy={busyIds.includes(approval.id)} error={errors[approval.id]} onDecision={decision => onDecision(approval, decision)} />;
  };
  return <section id="conversation-approvals" aria-label="会话审批" className="space-y-3">
    {pending.map(render)}
    {!!history.length && <details className="text-xs text-ink-muted"><summary className="cursor-pointer py-2">最近授权记录 · {history.length}</summary><div className="mt-2 space-y-2">{[...history].reverse().map(render)}</div></details>}
  </section>;
}
