import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { FileText, X } from "lucide-react";
import type { PlanDocument, PlanningSnapshot } from "@/types/api";

const labels: Record<string, string> = { draft: "起草中", submitted: "等待方案确认", approved: "已批准",
  starting: "准备执行", started: "已启动执行", blocked: "执行准备受阻", rejected: "请修改方案",
  withdrawn: "已放弃", superseded: "历史版本" };

export function PlanPreview({ snapshot, busy, error, onDecision, onExit, onRevise }: {
  snapshot?: PlanningSnapshot; busy: boolean; error?: string;
  onDecision: (plan: PlanDocument, decision: "approve" | "reject" | "withdraw" | "restore") => void;
  onExit: () => void; onRevise: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string>();
  const [copied, setCopied] = useState(false);
  const panelRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
      if (event.key === "Tab") {
        const controls = panelRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled),select,a[href]');
        if (!controls?.length) return;
        const first = controls[0]!, last = controls[controls.length - 1]!;
        if (event.shiftKey && (document.activeElement === first || document.activeElement === panelRef.current)) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener("keydown", handler);
    return () => { document.removeEventListener("keydown", handler); previous?.focus(); };
  }, [open]);
  const current = snapshot?.plans.find(p => p.id === snapshot.state.active_plan_id);
  const plan = snapshot?.plans.find(p => p.id === selected) ?? current;
  if (!snapshot || (!current && snapshot.state.mode === "off")) return null;
  const canDecide = plan?.id === current?.id;
  const button = "rounded-lg border border-line px-3 py-2 text-xs hover:bg-surface-strong disabled:opacity-40";
  const download = () => {
    if (!plan) return;
    const url = URL.createObjectURL(new Blob([plan.markdown], { type: "text/markdown;charset=utf-8" }));
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = `plan-r${plan.revision}.md`; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <>
    <div className="flex items-center gap-3 rounded-xl border border-accent/25 bg-accent/5 px-4 py-3" aria-live="polite">
      <FileText className="size-4 shrink-0 text-accent" />
      <div className="min-w-0 flex-1"><p className="truncate text-sm">{current?.title ?? "正在规划方案"}</p>
        <p className="text-xs text-ink-muted">{snapshot.state.target === "team" ? "团队方案" : "实施方案"} · {current ? `第 ${current.revision} 版 · ${labels[current.status]}` : "先调查，再提交预览"}</p></div>
      <button className={button} onClick={() => { setSelected(undefined); setOpen(true); }}>查看方案</button>
      {current?.status === "rejected" && <button className={`${button} shrink-0`} disabled={busy}
        title="取消修改，恢复原方案；恢复后仍需确认才会执行" onClick={() => onDecision(current, "restore")}>取消修改</button>}
      {!current && <button className={button} disabled={busy} onClick={onExit}>退出规划</button>}
    </div>
    {open && <div className="fixed inset-0 z-50 flex justify-end bg-black/25" onClick={() => setOpen(false)}>
      <section ref={panelRef} tabIndex={-1} role="dialog" aria-modal="true" aria-label="方案预览" onClick={event => event.stopPropagation()}
        className="flex h-full w-full max-w-3xl flex-col bg-surface shadow-2xl outline-none">
        <header className="flex items-center gap-3 border-b border-line p-4"><h2 className="flex-1 font-semibold">方案预览</h2>
          {snapshot.plans.length > 0 && <select aria-label="方案版本" value={plan?.id ?? ""} onChange={event => setSelected(event.target.value)} className="rounded border border-line bg-surface p-1 text-sm">
            {snapshot.plans.map(p => <option key={p.id} value={p.id}>第 {p.revision} 版 · {labels[p.status]}</option>)}
          </select>}
          <button aria-label="关闭方案预览" onClick={() => setOpen(false)}><X className="size-5" /></button></header>
        <div className="min-h-0 flex-1 overflow-y-auto p-5 sm:p-8">
          {plan ? <><div className="markdown answer-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{plan.markdown}</ReactMarkdown></div>
            {plan.payload.task_snapshot && <details className="mt-6 rounded-xl border border-line p-4"><summary className="cursor-pointer text-sm">团队任务与执行约束</summary>
              <div className="mt-3 space-y-4">{plan.payload.task_snapshot.map(({ task }) => <article key={task.id} className="border-t border-line pt-3">
                <h3 className="text-sm font-medium">#{task.id} · {task.subject}</h3>
                <p className="mt-1 whitespace-pre-wrap text-xs text-ink-muted">{task.description}</p>
                <p className="mt-2 text-xs">修改范围：{task.metadata?.write_scopes?.join("、") || "只读调查"}</p>
                {Boolean(task.blockedBy?.length) && <p className="mt-1 text-xs">依赖任务：{task.blockedBy!.join("、")}</p>}
                {task.metadata?.validation_commands?.map(command => <code key={command} className="mt-1 block break-all text-xs text-ink-muted">验证：{command}</code>)}
              </article>)}</div></details>}
          </> : <p className="text-ink-muted">方案尚未保存。Agent 保存草稿后会自动显示在这里。</p>}
          {(error || plan?.error) && <p role="alert" className="mt-4 rounded-lg bg-danger/10 p-3 text-sm text-danger">{error || plan?.error}</p>}
          {plan?.read_only && <p className="mt-3 text-sm text-ink-muted">此方案在只读保护下提交，需关闭保护并重新提交后执行。</p>}
        </div>
        <footer className="flex flex-wrap gap-2 border-t border-line p-4">
          <button className={button} disabled={!plan} onClick={() => { if (plan) void navigator.clipboard.writeText(plan.markdown).then(() => setCopied(true)).catch(() => setCopied(false)); }}>{copied ? "已复制" : "复制"}</button>
          <button className={button} disabled={!plan} onClick={download}>下载 Markdown</button>
          <div className="flex-1" />
          {plan && canDecide && ["submitted", "blocked", "approved"].includes(plan.status) && <button className={`${button} bg-accent text-white`} disabled={busy || plan.read_only}
            onClick={() => onDecision(plan, "approve")}>{plan.status === "blocked" ? "重试启动" : "批准并执行"}</button>}
          {plan && canDecide && plan.status === "submitted" && <button className={button} disabled={busy} onClick={() => { onDecision(plan, "reject"); setOpen(false); onRevise(); }}>修改方案</button>}
          {plan && canDecide && plan.status === "rejected" && <button className={button} disabled={busy}
            onClick={() => onDecision(plan, "restore")}>取消修改，恢复原方案</button>}
          {snapshot.state.mode === "planning" && canDecide && <button className={button} disabled={busy} onClick={onExit}>放弃方案</button>}
        </footer>
      </section>
    </div>}
  </>;
}
