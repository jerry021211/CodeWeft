import { useEffect, useMemo, useState } from "react";
import type { RunViewState } from "@/store/runStore";
import { collectRunMetrics, contextPressure, contextWindowSource, runElapsedMs } from "@/lib/runMetrics";
import { asNumber, cx, formatDuration, formatTime, isRunActive } from "@/lib/utils";

const number = (value: unknown) => {
  const numeric = asNumber(value);
  return numeric == null ? "不可用" : numeric.toLocaleString("zh-CN");
};

function Row({ label, value }: { label: string; value: string }) {
  return <div className="grid grid-cols-[auto_minmax(0,1fr)] gap-3 py-2 text-[11px]"><dt className="text-ink-muted">{label}</dt><dd className="min-w-0 break-words text-right font-mono text-ink" title={value}>{value}</dd></div>;
}

export function RunMetricsPanel({ run, workspace }: { run: RunViewState; workspace?: string }) {
  const metrics = useMemo(() => collectRunMetrics(run.events), [run.events]);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!isRunActive(run.status)) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [run.runId, run.status]);
  const elapsed = runElapsedMs(metrics, run.status, now);
  return <div className="rounded-2xl border border-line bg-surface-muted px-3.5 py-1">
    <dl className="divide-y divide-line">
      <Row label="运行耗时" value={elapsed == null ? run.status === "queued" ? "排队中" : "不可用" : formatDuration(elapsed)} />
      <Row label="模型调用" value={number(metrics.modelCalls)} />
      <Row label="工具执行" value={number(metrics.toolCalls)} />
      <Row label="工具失败" value={number(metrics.failedTools)} />
      <Row label="工作区" value={workspace ?? "不可用"} />
      <Row label="会话 ID" value={metrics.conversationId ?? "不可用"} />
    </dl>
    <p className="border-t border-line py-2 text-[10px] leading-4 text-ink-faint">本轮统计包含子 Agent；工具按已开始的调用计数，未执行的审批拒绝不计入。</p>
  </div>;
}

export function ContextMetricsPanel({ run }: { run: RunViewState }) {
  const metrics = useMemo(() => collectRunMetrics(run.events), [run.events]);
  const event = metrics.context;
  if (!event) return <div className="rounded-2xl border border-dashed border-line p-3 text-[11px] leading-5 text-ink-muted">尚无上下文快照。新运行会在主 Agent 准备模型请求后显示；旧记录可能未采集这些指标。</div>;
  const p = event.payload;
  const pressure = contextPressure(event);
  const tokenMode = pressure.ratio != null;
  const ratio = pressure.ratio ?? (pressure.maxChars && pressure.chars != null ? pressure.chars / pressure.maxChars : undefined);
  const marker = tokenMode ? pressure.nearRatio : pressure.maxChars && pressure.threshold != null ? pressure.threshold / pressure.maxChars : undefined;
  const label = pressure.blocked ? "请求超限" : pressure.near ? "接近压缩阈值" : pressure.knownBudget ? "预算内" : "状态未知";
  return <div className="overflow-hidden rounded-2xl border border-line">
    <div className="bg-surface-muted p-3.5">
      <div className="mb-3 flex items-center justify-between gap-2 text-[11px]"><span className="text-ink-muted">{tokenMode ? "窗口压力 · 本地估算" : "请求字符预算"}</span><span className={cx("font-medium", pressure.blocked ? "text-danger" : pressure.near ? "text-warning" : "text-accent")}>{label}</span></div>
      {ratio != null && <div role="progressbar" aria-label={tokenMode ? "上下文窗口占用估算（含输出预留）" : "请求字符预算占用"} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(Math.min(1, Math.max(0, ratio)) * 100)} aria-valuetext={`${(ratio * 100).toFixed(1)}%`} className="relative h-2 rounded-full bg-surface-strong">
        <div className={cx("h-full rounded-full transition-all", pressure.blocked ? "bg-danger" : pressure.near ? "bg-warning" : "bg-accent")} style={{ width: `${Math.min(100, Math.max(0, ratio * 100))}%` }} />
        {marker != null && <span className="absolute -top-1 h-4 w-0.5 bg-warning" style={{ left: `${Math.min(100, Math.max(0, marker * 100))}%` }} title="压缩触发阈值" />}
      </div>}
      <p className="mt-2 font-mono text-[11px] text-ink">{tokenMode ? `${number(p.estimated_total_tokens)} / ${number(pressure.window)} tokens` : `${number(pressure.chars)} / ${number(pressure.maxChars)} 字符`}{ratio != null && ` · ${(ratio * 100).toFixed(1)}%`}</p>
      <p className="mt-1 text-[10px] leading-4 text-ink-faint">{tokenMode ? "含输入估算与输出预留；阈值线仅表示当前预算维度。" : "暂未取得模型窗口；此处仅显示字符预算，不代表 Token 窗口。"}</p>
    </div>
    <dl className="divide-y divide-line px-3.5">
      <Row label="输入 Token（估算）" value={number(p.estimated_prompt_tokens)} />
      <Row label="输出预留" value={number(p.output_reserve_tokens)} />
      <Row label="窗口来源" value={contextWindowSource(event)} />
      {p.context_window_source === "model_api" && typeof p.context_window_model === "string" && <Row label="接口模型" value={p.context_window_model} />}
      {pressure.window != null && pressure.window > 0 && pressure.nearRatio != null && <Row label="Token 压缩阈值" value={`${number(Math.ceil(pressure.window * pressure.nearRatio))} (${(pressure.nearRatio * 100).toFixed(0)}%)`} />}
      <Row label="请求字符 / 压缩阈值" value={`${number(pressure.chars)} / ${number(pressure.threshold)}`} />
      <Row label="历史消息 / 请求消息" value={`${number(p.canonical_messages)} / ${number(p.projected_messages)}`} />
      <Row label="本轮压缩次数" value={number(metrics.compactions)} />
      <Row label="会话摘要版本" value={number(p.summary_revision)} />
      <Row label="已折叠历史消息" value={number(p.compacted_message_count)} />
      <Row label="裁剪工具结果" value={number(p.projected_tool_results)} />
      <Row label="工具结果减少字符" value={number(p.tool_result_chars_saved)} />
    </dl>
    <p className="border-t border-line px-3.5 py-2 text-[10px] leading-4 text-ink-faint">主 Agent 最近请求 · {formatTime(event.occurred_at)}。裁剪量仅统计本次保留的工具结果，不累计重复计算，也不包含摘要折叠的历史。</p>
  </div>;
}
