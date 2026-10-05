import type { TeamSnapshot, TokenUsage } from "@/types/api";
import type { RunViewState } from "@/store/runStore";
import { formatNumber, tokenTotal } from "@/lib/utils";

/** Do not show a finished, unrelated team as the current single-agent run. */
export function teamUsageForRun(run?: RunViewState, team?: TeamSnapshot) {
  if (!team) return undefined;
  const active = !["completed", "failed", "cancelled", "closed_with_unmerged_candidates"].includes(team.team.state);
  return !run || active || run.runId === team.team.root_run_id || run.events.some(event =>
    event.payload.team_run_id === team.team.id || team.agents.some(agent => agent.id === event.agent_id)
  ) ? team : undefined;
}

export function TokenUsagePanel({ run, team, model }: { run?: RunViewState; team?: TeamSnapshot; model?: string | null }) {
  // Team snapshots are persisted aggregates, refreshed independently of the
  // planning run's SSE stream (which can end while workers are still running).
  const usage = team?.usage ?? run?.usage ?? {};
  const models = Object.keys(team?.usage.by_model ?? {});
  const modelLabel = team ? (models.join("、") || "—") : usage.model || model || "—";
  const calls = team ? usage.model_calls ?? 0 : run?.usageByCall.length || usage.model_calls || 0;
  return <div className="overflow-hidden rounded-2xl border border-line">
    <div className="bg-surface-muted px-3.5 py-3">
      <div className="text-[9px] uppercase tracking-wider text-ink-faint">{team ? "团队累计 · Team total" : "本轮累计 · Run total"}</div>
      <div className="mt-1 flex items-end justify-between gap-2">
        <div className="text-xl font-semibold tracking-tight text-ink">{formatNumber(tokenTotal(usage))}</div>
        <span className="max-w-36 truncate font-mono text-[9px] text-ink-muted" title={modelLabel}>{modelLabel}</span>
      </div>
    </div>
    <UsageMetrics usage={usage} />
    <div className="border-t border-line px-3 py-2 text-[9px] text-ink-muted">
      已记录 {calls} 次模型调用{usage.estimated ? " · 含估算值" : ""}
      {!!usage.unavailable_calls && <div className="mt-1 text-warning">{usage.unavailable_calls} 次调用尚无 usage，总量暂不完整。</div>}
      {team && <div className="mt-1">包含规划、主 Agent 后续轮次、成员及辅助调用；重试按实际调用计入。</div>}
    </div>
    {team && <details className="border-t border-line" open>
      <summary className="cursor-pointer px-3 py-2 text-[10px] font-medium text-ink">按 Agent 查看</summary>
      {Object.entries(team.usage.by_agent ?? {}).map(([id, value]) => {
        const agent = team.agents.find(item => item.id === id);
        const label = id === team.team.lead_agent_id || agent?.role === "lead" ? "主 Agent"
          : agent?.name || (id === "agent_root" ? "主 Agent（规划）" : id);
        return <div key={id} className="border-t border-line">
          <div className="flex justify-between gap-2 px-3 py-2 text-[10px] text-ink" title={id}>
            <span className="min-w-0 truncate">{label}</span>
            <span className="shrink-0">{formatNumber(tokenTotal(value))} · {value.model_calls ?? 0} 次</span>
          </div>
          <UsageMetrics usage={value} />
          {!!value.unavailable_calls && <div className="px-3 py-1 text-[9px] text-warning">{value.unavailable_calls} 次尚无 usage</div>}
        </div>;
      })}
      {!team.usage.by_agent && <div className="px-3 pb-2 text-[9px] text-ink-muted">暂无 Agent 明细</div>}
    </details>}
  </div>;
}

function UsageMetrics({ usage }: { usage: TokenUsage }) {
  const metrics = [
    ["未缓存输入", usage.input_tokens], ["缓存读取", usage.cache_read_input_tokens],
    ["缓存命中率", usage.cache_hit_ratio == null ? null : `${(usage.cache_hit_ratio * 100).toFixed(1)}%`],
    ["输出", usage.output_tokens],
    ...(usage.cache_creation_input_tokens ? [["缓存写入", usage.cache_creation_input_tokens]] : []),
  ];
  return <div className="grid grid-cols-2 divide-x divide-y divide-line border-t border-line">
    {metrics.map(([label, value]) => <div key={label} className="px-3 py-2">
      <div className="text-[9px] text-ink-faint">{label}</div>
      <div className="mt-0.5 font-mono text-[11px] text-ink">{typeof value === "number" ? formatNumber(value) : value ?? "不可用"}</div>
    </div>)}
  </div>;
}
