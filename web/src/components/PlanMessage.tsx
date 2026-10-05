import { CheckCircle2, FileText } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { PlanDocument } from "@/types/api";

const statuses: Record<string, string> = {
  submitted: "方案已完成，等待确认", approved: "方案已批准", starting: "正在准备执行",
  started: "已按此方案启动执行", blocked: "方案已完成，执行准备受阻",
  rejected: "已请求修改方案", withdrawn: "方案已放弃", superseded: "历史方案，已有新版本",
};

export function PlanMessage({ plan, current, busy, error, onDecision }: {
  plan: PlanDocument; current: boolean; busy?: boolean; error?: string;
  onDecision?: (plan: PlanDocument, decision: "approve" | "reject" | "restore") => void;
}) {
  const canApprove = current && ["submitted", "approved", "blocked"].includes(plan.status);
  const button = "rounded-lg px-3 py-2 text-xs font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-50";
  return <article aria-label={`实施方案：${plan.title}，第 ${plan.revision} 版`} className="min-w-0 rounded-2xl border border-line bg-surface">
    <header className="flex items-start gap-3 border-b border-line px-4 py-4 sm:px-5">
      <FileText className="mt-0.5 size-4 shrink-0 text-accent" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <h2 className="break-words text-sm font-semibold text-ink">{plan.title}</h2>
        <p role="status" className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-ink-muted">
          {plan.status === "submitted" && <CheckCircle2 className="size-3.5 text-success" aria-hidden="true" />}
          {statuses[plan.status] ?? plan.status}<span aria-hidden="true">·</span>第 {plan.revision} 版
        </p>
      </div>
    </header>
    <div className="markdown answer-markdown min-w-0 break-words px-4 py-5 sm:px-5">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{plan.markdown}</ReactMarkdown>
    </div>
    {current && (error || plan.error) && <p role="alert" className="mx-4 mb-4 rounded-lg bg-danger/10 p-3 text-xs text-danger">{error || plan.error}</p>}
    {current && plan.read_only && canApprove && <p className="px-4 pb-4 text-xs text-ink-muted">此方案在只读保护下提交，需退出规划、更改执行方式并重新提交后执行。</p>}
    {onDecision && current && plan.status === "rejected" && <footer className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3 sm:px-5">
      <button type="button" disabled={busy} onClick={() => onDecision(plan, "restore")}
        className={`${button} text-ink hover:bg-surface-strong`}>取消修改，恢复原方案</button>
      <span className="text-[11px] text-ink-faint">恢复后仍需确认才会执行</span>
    </footer>}
    {onDecision && canApprove && <footer className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3 sm:px-5">
      <button type="button" disabled={busy || plan.read_only} onClick={() => onDecision(plan, "approve")}
        className={`${button} bg-ink text-surface hover:opacity-80`}>{plan.status === "blocked" ? "重试启动" : "批准并执行"}</button>
      {plan.status === "submitted" && <button type="button" disabled={busy} onClick={() => onDecision(plan, "reject")}
        className={`${button} text-ink-muted hover:bg-surface-strong hover:text-ink`}>修改方案</button>}
      <span className="text-[11px] text-ink-faint">确认后开始实现</span>
    </footer>}
  </article>;
}
