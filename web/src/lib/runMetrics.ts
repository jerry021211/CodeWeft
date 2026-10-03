import type { RunEvent, RunStatus } from "@/types/api";
import { asNumber, isRunActive } from "@/lib/utils";

const canonical = (type: string) => type.replace(/[.\-:/]+/g, "_");
const terminal = new Set(["run_completed", "run_failed", "run_cancelled", "run_interrupted"]);

export function collectRunMetrics(events: RunEvent[]) {
  const models = new Set<string>();
  const tools = new Set<string>();
  const failedTools = new Set<string>();
  let startedAt: string | undefined;
  let endedAt: string | undefined;
  let conversationId: string | undefined;
  let context: RunEvent | undefined;
  let compactions = 0;
  const seen = new Set<string>();
  for (const event of events) {
    const key = `${event.run_id}:${event.seq}`;
    if (seen.has(key)) continue;
    seen.add(key);
    conversationId = event.conversation_id;
    const type = canonical(event.type);
    const payload = event.payload;
    const callKey = `${event.agent_id}:${payload.call_id ?? payload.model_call_id ?? event.id}`;
    const toolKey = `${event.agent_id}:${payload.tool_use_id ?? payload.tool_call_id ?? event.id}`;
    if (type === "model_started") models.add(callKey);
    if (type === "tool_started") tools.add(toolKey);
    if (type === "tool_failed") failedTools.add(toolKey);
    if (!event.parent_agent_id) {
      if (type === "run_started") startedAt = event.occurred_at;
      if (terminal.has(type)) endedAt = event.occurred_at;
      if (type === "context_compacted") compactions++;
      if ((type === "context_request_projected" || type === "context_request_blocked") && (!context || context.seq < event.seq)) context = event;
    }
  }
  return { modelCalls: models.size, toolCalls: tools.size, failedTools: failedTools.size, startedAt, endedAt, conversationId, context, compactions };
}

export function runElapsedMs(metrics: ReturnType<typeof collectRunMetrics>, status: RunStatus, now: number) {
  const start = Date.parse(metrics.startedAt ?? "");
  const end = metrics.endedAt ? Date.parse(metrics.endedAt) : isRunActive(status) ? now : NaN;
  return Number.isFinite(start) && Number.isFinite(end) ? Math.max(0, end - start) : undefined;
}

export function contextPressure(event?: RunEvent) {
  const p = event?.payload ?? {};
  const window = asNumber(p.context_window_tokens);
  const total = asNumber(p.estimated_total_tokens);
  const chars = asNumber(p.text_request_chars) ?? asNumber(p.request_chars);
  const softPromptTokens = asNumber(p.effective_soft_prompt_tokens);
  const outputReserve = asNumber(p.output_reserve_tokens) ?? 0;
  const nearRatio = window != null && window > 0 && softPromptTokens != null
    ? (softPromptTokens + outputReserve) / window : asNumber(p.near_context_ratio);
  const ratio = window != null && window > 0 && total != null ? total / window : undefined;
  const blocked = event ? canonical(event.type) === "context_request_blocked" : false;
  const near = ratio != null && nearRatio != null && ratio >= nearRatio;
  const knownBudget = ratio != null;
  return { window, ratio, nearRatio, blocked, near, chars, knownBudget };
}

export function contextWindowSource(event: RunEvent) {
  const p = event.payload;
  if (p.context_window_source === "model_api") return "模型接口";
  const reasons: Record<string, string> = {
    missing_credentials: "缺少查询凭据", invalid_endpoint: "接口地址无效",
    unauthorized: "接口拒绝访问", unsupported_endpoint: "服务商不支持模型查询",
    request_failed: "接口查询失败", invalid_response: "接口返回格式不支持",
    missing_window: "接口未返回窗口大小", model_not_found: "接口未找到当前模型",
    timeout: "接口查询超时",
  };
  if (typeof p.context_window_reason === "string") return reasons[p.context_window_reason] ?? "接口查询失败";
  return asNumber(p.context_window_tokens) ? "历史配置记录" : "旧记录未采集";
}
