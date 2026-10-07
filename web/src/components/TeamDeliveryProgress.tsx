import { useState } from "react";
import { Check, CircleAlert, GitBranch } from "lucide-react";
import type { TeamSnapshot } from "@/types/api";

export type ResolveIntegration = (id: string, action: "retry" | "repair" | "resume", reason: string) => void;
const blocked = new Set(["validation_failed", "conflicted", "interrupted", "recovery_required"]);
const labels: Record<string, string> = { preparing: "试合并中", validating: "正在检查", publishing: "正在发布集成版本", applying: "正在回写本地", published: "已进入集成版本", delivered: "已回写本地，等待你试用", superseded: "已安排后续处理", conflicted: "存在合并冲突", validation_failed: "检查未通过", interrupted: "执行已中断", recovery_required: "需要核验执行现场" };
type Operation = NonNullable<TeamSnapshot["integrations"]>[number];

function OperationCard({ operation, team, busy, onResolve }: { operation: Operation; team: TeamSnapshot; busy?: boolean; onResolve?: ResolveIntegration }) {
  const [acknowledged, setAcknowledged] = useState(false);
  const failed = operation.validations.find(v => v.status !== "passed");
  const diagnostic = operation.result?.diagnostic ?? failed?.diagnostic;
  const candidate = team.candidates.find(c => c.id === operation.candidate_id);
  const task = team.tasks.find(t => t.task.id === candidate?.task_id)?.task;
  const problem = blocked.has(operation.status);
  const environment = ["environment", "dependency_install", "timeout"].includes(diagnostic?.category ?? "");
  const canAct = team.team.state === "running" && !busy;
  return <article className={`rounded-xl border p-3 sm:p-4 ${problem ? "border-warning/30 bg-warning/5" : "border-line bg-surface"}`}>
    <div className="flex items-start gap-2.5">
      {problem ? <CircleAlert className="mt-0.5 size-4 shrink-0 text-warning" /> : <GitBranch className="mt-0.5 size-4 shrink-0 text-accent" />}
      <div className="min-w-0 flex-1">
        <p className="text-xs font-semibold text-ink">{operation.kind === "delivery" ? "最终验收与本地回写" : `任务 #${candidate?.task_id ?? "—"} · ${task?.subject ?? "成果集成"}`}</p>
        <p role="status" className="mt-1 text-xs leading-5 text-ink-muted">{diagnostic?.summary ?? labels[operation.status] ?? operation.status}</p>
        {problem && <p className="mt-1 text-xs leading-5 text-ink-muted">{environment ? "先处理执行环境；成果已保留，无需因此重写业务代码。" : operation.status === "recovery_required" ? "先核验现场和旧进程，避免重复执行结果不明的操作。" : "Lead 负责检查日志和安排处理；失败结果不会作为已通过版本发布。"}</p>}
        {operation.result?.pending_checks?.map(check => <p key={check.id} className="mt-1 text-xs text-ink-faint">待执行：{check.id} · {check.reason}</p>)}
      </div>
    </div>
    <details className="mt-3 text-xs text-ink-muted">
      <summary className="cursor-pointer text-accent">查看检查步骤与日志 · {operation.validations.length} 项记录</summary>
      <div className="mt-2 space-y-2">{operation.validations.map((v, i) => <div key={i} className="rounded-lg bg-surface-muted p-2.5">
        <div className="mb-1 font-medium">{v.status === "passed" ? "已通过" : v.status === "timed_out" ? "执行超时" : "未通过"}{v.exit_code != null ? ` · 退出码 ${v.exit_code}` : ""}</div>
        <code className="block break-all whitespace-pre-wrap">{v.command}</code>
        {v.output_excerpt && <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words border-t border-line pt-2">{v.output_excerpt}</pre>}
      </div>)}</div>
      {operation.error && <p className="mt-2 break-words">原始记录：{operation.error}</p>}
      <p className="mt-2 break-all text-ink-faint">工作现场：{operation.worktree_path}</p>
    </details>
    {problem && onResolve && <div className="mt-3 space-y-2 border-t border-line pt-3">
      {operation.status === "recovery_required" ? <>
        <label className="flex items-start gap-2 text-xs leading-5 text-ink-muted"><input type="checkbox" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} />已检查现场并确认旧验证进程停止</label>
        <button disabled={!canAct || !acknowledged} onClick={() => onResolve(operation.id, "resume", "用户核验现场及旧验证进程后请求恢复")} className="rounded-lg border border-line bg-surface px-3 py-2 text-xs disabled:opacity-40">检查并恢复</button>
      </> : <div className="flex flex-wrap gap-2">
        <button disabled={!canAct} onClick={() => onResolve(operation.id, "retry", "用户请求在当前运行环境重新执行验证，保留原成果")} className="rounded-lg border border-line bg-surface px-3 py-2 text-xs disabled:opacity-40">环境处理后重新验证</button>
        {!environment && operation.kind === "candidate" && <button disabled={!canAct} onClick={() => onResolve(operation.id, "repair", `请根据失败日志诊断并修复：${failed?.command ?? operation.error ?? "集成冲突"}`)} className="rounded-lg border border-line bg-surface px-3 py-2 text-xs disabled:opacity-40">交给 Lead 安排修复</button>}
      </div>}
    </div>}
  </article>;
}

export function TeamDeliveryProgress({ team, busy, onResolve }: { team: TeamSnapshot; busy?: boolean; onResolve?: ResolveIntegration }) {
  const operations = team.integrations ?? [];
  const active = operations.filter(op => !["published", "delivered", "superseded"].includes(op.status));
  const history = operations.filter(op => !active.includes(op));
  if (!operations.length && !team.candidates.length) return null;
  return <section aria-label="成果审查与验证" className="space-y-3">
    <div className="flex items-center gap-2 text-xs font-semibold text-ink"><Check className="size-4 text-accent" />成果审查与验证</div>
    {!active.length && <p className="text-xs leading-5 text-ink-muted">{team.team.state === "completed" ? "成果已交付，可在本地试用。" : "通过当前阶段检查后继续集成；完整验收在所需成果到齐后执行。"}</p>}
    {active.map(op => <OperationCard key={op.id} operation={op} team={team} busy={busy} onResolve={onResolve} />)}
    <details className="text-xs text-ink-muted"><summary className="cursor-pointer">成果说明与审查记录 · {team.candidates.length} 份</summary>
      <div className="mt-2 space-y-2">{team.candidates.map(c => <details key={c.id} className="rounded-lg border border-line p-3">
        <summary className="cursor-pointer">任务 #{c.task_id} · {({ submitted: "等待 Lead 审查", accepted: c.user_approval_required && !c.user_decision ? "等待显式审批策略确认" : "Lead 已接受，等待验证", validating: "正在验证", committed: "已生成本地提交", rework: "已安排返工", validation_failed: "验证未通过" } as Record<string, string>)[c.status] ?? c.status} · {new Set([...(c.changed_files ?? []), ...(c.untracked_files ?? [])]).size} 个文件</summary>
        <p className="mt-2 whitespace-pre-wrap break-words leading-5">{c.summary}</p>
        {!!c.known_risks?.length && <div className="mt-2"><p className="font-medium">成员报告的限制与待核验事项（不等于已发现故障）</p><ul className="mt-1 list-disc space-y-1 pl-4">{c.known_risks.map((risk, i) => <li key={i}>{risk}</li>)}</ul></div>}
      </details>)}</div>
    </details>
    {!!history.length && <details className="text-xs text-ink-muted"><summary className="cursor-pointer">已完成与历史操作 · {history.length}</summary><div className="mt-2 space-y-2">{history.map(op => <OperationCard key={op.id} operation={op} team={team} />)}</div></details>}
  </section>;
}
