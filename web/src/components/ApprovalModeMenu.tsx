import { useEffect, useId, useRef, useState } from "react";
import { Check, ChevronDown, Hand, ShieldAlert, ShieldCheck } from "lucide-react";
import type { ApprovalMode } from "@/types/api";
import { cx } from "@/lib/utils";

const choices = [
  { value: "ask", label: "请求批准", detail: "联网、外部工具和风险操作执行前询问", icon: Hand },
  { value: "auto", label: "自动审批", detail: "常规操作自动执行，有风险或无法判断时询问", icon: ShieldCheck },
  { value: "full", label: "完全访问", detail: "工具执行不再逐次询问，仍遵守只读及项目限制", icon: ShieldAlert },
] as const;

type Props = { value?: ApprovalMode; disabled?: boolean; onChange?: (mode: ApprovalMode) => void };

export function ApprovalModeMenu({ value = "auto", disabled, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  const selected = choices.find(item => item.value === value)!;
  const close = () => { setOpen(false); trigger.current?.focus(); };
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  useEffect(() => {
    if (!open) return;
    root.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')?.focus();
    const dismiss = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [open]);
  return <div ref={root} className="relative" onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
  }} onKeyDown={event => { if (event.key === "Escape") { event.stopPropagation(); close(); } }}>
    <button ref={trigger} type="button" aria-label={`审批模式：${selected.label}`} aria-haspopup="dialog"
      aria-expanded={open} aria-controls={open ? id : undefined} disabled={disabled || !onChange}
      title={selected.detail} onClick={() => setOpen(current => !current)}
      className={cx("inline-flex h-8 items-center gap-1.5 rounded-lg px-2 text-xs hover:bg-surface-strong disabled:opacity-50", value === "full" ? "text-warning" : "text-ink-muted")}>
      <selected.icon className="size-3.5" />{selected.label}<ChevronDown className="size-3" />
    </button>
    {open && <div id={id} role="dialog" aria-label="如何批准 Agent 操作" className="absolute bottom-full left-0 z-40 mb-2 w-80 max-w-[calc(100vw-3rem)] rounded-2xl border border-line bg-surface p-2 shadow-panel">
      <p className="px-3 py-2 text-xs text-ink-faint">如何批准 Agent 操作？</p>
      {choices.map(item => <button key={item.value} type="button" aria-pressed={value === item.value}
        disabled={disabled} onClick={() => { onChange?.(item.value); close(); }}
        className={cx("flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-surface-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40", item.value === "full" ? "text-warning" : "text-ink")}>
        <item.icon className="size-4 shrink-0" /><span className="flex-1"><span className="block text-sm">{item.label}{item.value === "auto" && "（默认）"}</span>
          <span className="mt-1 block text-xs leading-5 text-ink-muted">{item.detail}</span></span>
        {value === item.value && <Check className="size-4 shrink-0" />}
      </button>)}
    </div>}
  </div>;
}
