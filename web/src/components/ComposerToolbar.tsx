import { ArrowUp, Brain, ChevronDown, FileText, Globe, Plus, Square, X } from "lucide-react";
import { cx } from "@/lib/utils";
import { Spinner } from "@/components/ui";
import { ComposerModeMenu } from "@/components/ComposerModeMenu";

type Props = {
  planMode?: boolean; planLocked?: boolean; onPlanModeChange?: (value: boolean) => void;
  busy: boolean; active: boolean; cancelling: boolean; sending?: boolean; canSend: boolean;
  readOnly?: boolean; useTeam?: boolean; teamEnabled?: boolean; teamLeadActive?: boolean;
  teamState?: string;
  runtimeModel?: string | null; reasoningEffort?: string; reasoningOptions?: string[]; reasoningDefault?: string | null;
  webSearch?: boolean; webSearchAvailable?: boolean; attachmentDisabled: boolean; attachmentsReading: boolean;
  onReadOnlyChange: (value: boolean) => void; onTeamChange?: (value: boolean) => void;
  onReasoningChange?: (value: string) => void; onWebSearchChange?: (value: boolean) => void;
  onOpenSettings?: () => void; onAttach?: () => void; onSend: () => void; onCancel: () => void;
};
const control = "inline-flex h-8 min-w-0 items-center gap-1.5 rounded-lg text-xs text-ink-muted transition hover:bg-surface-strong hover:text-ink";
const iconButton = "relative inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-ink-muted transition hover:bg-surface-strong hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-35";

export function ComposerToolbar(props: Props) {
  const mode = props.teamLeadActive || props.useTeam ? "team" : props.readOnly ? "read" : "agent";
  const reasoning = props.reasoningEffort ?? "default";
  const reasoningLabel = (value: string) => ({ default: `默认${props.reasoningDefault ? ` · ${props.reasoningDefault}` : ""}`, none: "关闭思考", low: "低", medium: "中", high: "高", max: "最高" } as Record<string, string>)[value] ?? value;
  return <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-2 px-2.5 pb-2.5 pt-1" aria-label="消息选项">
    <div className="flex min-w-0 flex-wrap items-center gap-0.5">
      <ComposerModeMenu mode={mode} busy={props.busy} teamEnabled={props.teamEnabled} teamLeadActive={props.teamLeadActive} teamState={props.teamState}
        planMode={props.planMode} planLocked={props.planLocked} onPlanModeChange={props.onPlanModeChange}
        onModeChange={value => { props.onReadOnlyChange(value === "read"); props.onTeamChange?.(value === "team"); }} />
      {props.planMode && <span className="mx-1 inline-flex h-6 items-center gap-1.5 rounded-md bg-accent/10 px-2 text-[11px] font-medium text-accent">
        <FileText className="size-3" aria-hidden="true" />规划
        {props.useTeam ? <span className="text-[10px] font-normal opacity-70">自动</span> : <button type="button" aria-label="退出规划模式"
          title="退出规划模式" disabled={props.busy} onClick={() => props.onPlanModeChange?.(false)}
          className="-mr-1 grid size-4 place-items-center rounded hover:bg-accent/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:opacity-50"><X className="size-3" /></button>}
      </span>}
      <button type="button" onClick={props.onOpenSettings} disabled={!props.onOpenSettings || props.busy}
        aria-label="打开模型设置" title={`当前模型：${props.runtimeModel || "未配置"} · 打开模型设置`}
        className={cx(control, "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 max-w-[130px] px-2 disabled:cursor-not-allowed disabled:opacity-50 sm:max-w-[200px]")}>
        <span className="truncate">{props.runtimeModel || "选择模型"}</span><ChevronDown className="size-3 shrink-0 text-ink-faint" />
      </button>
      <label className={cx(control, "relative pl-2")}>
        <Brain className="pointer-events-none size-3.5 shrink-0 text-ink-faint" />
        <select aria-label="推理等级" value={reasoning}
          disabled={props.busy || props.teamLeadActive || !props.reasoningOptions || props.reasoningOptions.length < 2}
          title={props.teamLeadActive ? "团队成员继承启动任务时的推理等级" : "推理强度"}
          onChange={event => props.onReasoningChange?.(event.target.value)}
          className="h-8 max-w-28 appearance-none rounded-lg bg-transparent pl-0.5 pr-5 text-xs outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-50">
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
