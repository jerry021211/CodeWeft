import { ArrowUp, Bot, Brain, ChevronDown, Globe, Plus, ShieldCheck, Square, Users } from "lucide-react";
import { cx } from "@/lib/utils";
import { Spinner } from "@/components/ui";

type Props = {
  busy: boolean; active: boolean; cancelling: boolean; sending?: boolean; canSend: boolean;
  readOnly?: boolean; useTeam?: boolean; teamEnabled?: boolean; teamLeadActive?: boolean;
  runtimeModel?: string | null; reasoningEffort?: string; reasoningOptions?: string[]; reasoningDefault?: string | null;
  webSearch?: boolean; webSearchAvailable?: boolean; attachmentDisabled: boolean; attachmentsReading: boolean;
  onReadOnlyChange: (value: boolean) => void; onTeamChange?: (value: boolean) => void;
  onReasoningChange?: (value: string) => void; onWebSearchChange?: (value: boolean) => void;
  onOpenSettings?: () => void; onAttach?: () => void; onSend: () => void; onCancel: () => void;
};
const control = "inline-flex h-8 min-w-0 items-center gap-1.5 rounded-lg text-xs text-ink-muted transition hover:bg-surface-strong hover:text-ink focus-within:ring-2 focus-within:ring-accent/30";
const iconButton = "relative inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-ink-muted transition hover:bg-surface-strong hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-35";

export function ComposerToolbar(props: Props) {
  const mode = props.teamLeadActive || props.useTeam ? "team" : props.readOnly ? "read" : "agent";
  const ModeIcon = mode === "team" ? Users : mode === "read" ? ShieldCheck : Bot;
  const reasoning = props.reasoningEffort ?? "default";
  const reasoningLabel = (value: string) => ({ default: `默认${props.reasoningDefault ? ` · ${props.reasoningDefault}` : ""}`, none: "关闭思考", low: "低", medium: "中", high: "高", max: "最高" } as Record<string, string>)[value] ?? value;
  return <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-2 px-2.5 pb-2.5 pt-1" aria-label="消息选项">
    <div className="flex min-w-0 flex-wrap items-center gap-0.5">
      <label className={cx(control, "relative pl-2", mode !== "agent" && "bg-accent/10 text-accent")}>
        <ModeIcon className="pointer-events-none size-3.5 shrink-0" />
        <select aria-label="执行方式" value={mode} disabled={props.busy || props.teamLeadActive}
          title={props.teamLeadActive ? "团队运行中，继续向 Lead 发送指令" : "Agent 执行任务；只读仅允许读取和搜索；团队由 Lead 分配任务"}
          onChange={event => { const value = event.target.value; props.onReadOnlyChange(value === "read"); props.onTeamChange?.(value === "team"); }}
          className="h-8 max-w-28 appearance-none rounded-lg bg-transparent pl-0.5 pr-5 text-xs font-medium outline-none disabled:cursor-not-allowed disabled:opacity-50">
          <option value="agent">Agent</option><option value="read">只读</option>
          {(props.teamEnabled || props.teamLeadActive) && <option value="team">{props.teamLeadActive ? "团队运行中" : "Agent Team"}</option>}
        </select><ChevronDown className="pointer-events-none absolute right-1 size-3 text-ink-faint" />
      </label>
      <span className="mx-1 h-3 w-px bg-line" aria-hidden />
      <button type="button" onClick={props.onOpenSettings} disabled={!props.onOpenSettings || props.busy}
        aria-label="打开模型设置" title={`当前模型：${props.runtimeModel || "未配置"} · 打开模型设置`}
        className={cx(control, "max-w-[130px] px-2 disabled:cursor-not-allowed disabled:opacity-50 sm:max-w-[200px]")}>
        <span className="truncate">{props.runtimeModel || "选择模型"}</span><ChevronDown className="size-3 shrink-0 text-ink-faint" />
      </button>
      <label className={cx(control, "relative pl-2")}>
        <Brain className="pointer-events-none size-3.5 shrink-0 text-ink-faint" />
        <select aria-label="推理等级" value={reasoning}
          disabled={props.busy || props.teamLeadActive || !props.reasoningOptions || props.reasoningOptions.length < 2}
          title={props.teamLeadActive ? "团队成员继承启动任务时的推理等级" : "推理强度"}
          onChange={event => props.onReasoningChange?.(event.target.value)}
          className="h-8 max-w-28 appearance-none rounded-lg bg-transparent pl-0.5 pr-5 text-xs outline-none disabled:cursor-not-allowed disabled:opacity-50">
          {(props.reasoningOptions ?? ["default"]).map(value => <option key={value} value={value}>{reasoningLabel(value)}</option>)}
        </select><ChevronDown className="pointer-events-none absolute right-1 size-3 text-ink-faint" />
      </label>
    </div>
    <div className="ml-auto flex shrink-0 items-center gap-1">
      {props.onAttach && <button type="button" aria-label="添加附件" title="添加图片、PDF、文本或音频" onClick={props.onAttach} disabled={props.attachmentDisabled || props.attachmentsReading} className={iconButton}>
        {props.attachmentsReading ? <Spinner className="size-3.5" /> : <Plus className="size-4" />}
      </button>}
      <button type="button" aria-label="联网搜索" aria-pressed={Boolean(props.webSearch)}
        disabled={props.busy || props.useTeam || props.teamLeadActive || !props.webSearchAvailable}
        title={props.useTeam || props.teamLeadActive ? "团队会话暂不支持联网搜索" : !props.webSearchAvailable ? "请配置 TAVILY_API_KEY 后启用联网搜索" : `联网搜索：${props.webSearch ? "已开启" : "已关闭"}`}
        onClick={() => props.onWebSearchChange?.(!props.webSearch)} className={cx(iconButton, props.webSearch && "bg-accent/10 text-accent")}>
        <Globe className="size-4" />{props.webSearch && <span className="absolute right-1 top-1 size-1.5 rounded-full bg-accent ring-2 ring-surface" />}
      </button>
      <span className="mx-1 h-4 w-px bg-line" aria-hidden />
      {props.active ? <button type="button" aria-label={props.cancelling ? "正在停止" : "停止生成"} title={props.cancelling ? "等待当前步骤结束" : "停止生成"} onClick={props.onCancel} disabled={props.cancelling}
        className="grid size-8 shrink-0 place-items-center rounded-full bg-ink text-surface transition hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50">
        {props.cancelling ? <Spinner className="size-3.5 text-surface" /> : <Square className="size-3 fill-current" />}
      </button> : <button type="button" aria-label="发送" title="发送消息（Enter）" onClick={props.onSend} disabled={!props.canSend}
        className="grid size-8 shrink-0 place-items-center rounded-full bg-ink text-surface transition hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:bg-surface-strong disabled:text-ink-faint">
        {props.sending ? <Spinner className="size-3.5 text-surface" /> : <ArrowUp className="size-4" />}
      </button>}
    </div>
  </div>;
}
