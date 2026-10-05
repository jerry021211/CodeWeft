import { useEffect, useId, useRef, useState } from "react";
import { Bot, Check, ChevronDown, FileText, ShieldCheck, Users } from "lucide-react";
import { cx } from "@/lib/utils";

type Props = {
  mode: "agent" | "read" | "team";
  busy: boolean;
  teamEnabled?: boolean;
  teamLeadActive?: boolean;
  teamState?: string;
  planMode?: boolean;
  planLocked?: boolean;
  onModeChange: (mode: "agent" | "read" | "team") => void;
  onPlanModeChange?: (enabled: boolean) => void;
};

export function ComposerModeMenu(props: Props) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const id = useId();
  const choices = [
    { value: "agent" as const, label: "Agent", detail: "直接开始实现，边做边验证", icon: Bot },
    { value: "read" as const, label: "只读", detail: "阅读、分析项目，保护源代码", icon: ShieldCheck },
    ...(props.teamEnabled || props.teamLeadActive ? [
      { value: "team" as const, label: "Agent Team", detail: "先确认方案，再由团队协作实现", icon: Users },
    ] : []),
  ];
  const selected = choices.find(choice => choice.value === props.mode) ?? choices[0]!;
  const Icon = selected.icon;
  const modeDisabled = props.busy || props.teamLeadActive || props.planLocked;
  const planDisabled = props.busy || props.teamLeadActive || props.mode === "team" || !props.onPlanModeChange;
  useEffect(() => {
    if (!open) return;
    (panel.current?.querySelector<HTMLElement>('button:not(:disabled)') ?? panel.current)?.focus();
    const dismiss = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [open]);
  useEffect(() => { if (props.busy) setOpen(false); }, [props.busy]);
  const close = () => { setOpen(false); trigger.current?.focus(); };

  return <div ref={root} className="relative shrink-0" onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
  }}>
    <button ref={trigger} type="button" aria-label="执行方式" aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? id : undefined}
      disabled={props.busy} onClick={() => setOpen(value => !value)}
      className={cx("inline-flex h-8 items-center gap-1.5 rounded-full px-2.5 text-xs font-medium text-ink transition-colors hover:bg-surface-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:opacity-50", open && "bg-surface-strong")}>
      <Icon className="size-3.5 text-ink-muted" aria-hidden="true" />
      {props.teamLeadActive ? ({ recovery_waiting: "团队等待恢复", paused: "团队已暂停", pausing: "团队正在暂停", planning: "团队规划中", waiting_approval: "团队等待确认", ready_for_manual_integration: "团队等待集成" } as Record<string, string>)[props.teamState ?? ""] ?? "团队运行中" : selected.label}
      <ChevronDown className={cx("size-3 text-ink-faint transition-transform", open && "rotate-180")} aria-hidden="true" />
    </button>
    {open && <div ref={panel} id={id} role="dialog" tabIndex={-1} aria-label="选择工作方式"
      className="absolute bottom-full left-0 z-40 mb-2 w-72 max-w-[calc(100vw-3rem)] rounded-2xl border border-line bg-surface p-1.5 shadow-panel outline-none"
      onKeyDown={event => {
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
        if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
          event.preventDefault();
          const items = Array.from(panel.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? []);
          if (!items.length) return;
          const current = items.indexOf(document.activeElement as HTMLButtonElement);
          const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
            : (current + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
          items[next]?.focus();
        }
      }}>
      <p className="px-3 pb-1.5 pt-2 text-[10px] font-medium text-ink-faint">工作方式</p>
      <div role="group" aria-label="执行方式选项">
        {choices.map(choice => <button key={choice.value} type="button" aria-pressed={props.mode === choice.value} disabled={modeDisabled}
          onClick={() => { props.onModeChange(choice.value); close(); }}
          className="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-surface-strong focus-visible:bg-surface-strong focus-visible:outline-none disabled:opacity-50">
          <choice.icon className="size-4 shrink-0 text-ink-muted" aria-hidden="true" />
          <span className="min-w-0 flex-1"><span className="block text-xs font-medium text-ink">{choice.label}</span>
            <span className="mt-0.5 block text-[11px] leading-4 text-ink-muted">{choice.detail}</span></span>
          {props.mode === choice.value && <Check className="size-3.5 shrink-0 text-ink" aria-hidden="true" />}
        </button>)}
      </div>
      <div className="mx-2 my-1 border-t border-line" />
      <button type="button" role="switch" aria-checked={Boolean(props.planMode)} disabled={planDisabled}
        onClick={() => { props.onPlanModeChange?.(!props.planMode); close(); }}
        className="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-surface-strong focus-visible:bg-surface-strong focus-visible:outline-none disabled:opacity-50">
        <FileText className="size-4 shrink-0 text-ink-muted" aria-hidden="true" />
        <span className="flex-1"><span className="block text-xs font-medium text-ink">先出方案</span>
          <span className="mt-0.5 block text-[11px] text-ink-muted">{props.mode === "team" ? "新团队自动开启，确认后开始执行" : "预览实施步骤，确认后开始执行"}</span></span>
        <span aria-hidden="true" className={cx("flex h-4 w-7 shrink-0 items-center rounded-full p-0.5 transition-colors", props.planMode ? "bg-ink" : "bg-line-strong")}>
          <span className={cx("size-3 rounded-full bg-surface shadow-sm transition-transform", props.planMode && "translate-x-3")} />
        </span>
      </button>
      {props.planLocked && <p className="px-3 pb-2 pt-1 text-[10px] leading-4 text-ink-faint">退出当前规划后可切换工作方式</p>}
    </div>}
  </div>;
}
