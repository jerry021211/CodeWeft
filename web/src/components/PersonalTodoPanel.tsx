import { useState } from "react";
import { Check, ChevronDown, ChevronUp, Circle, ListChecks } from "lucide-react";
import type { TodoItem } from "@/types/api";
import { Spinner } from "@/components/ui";
import type { TodoExecutionStatus } from "@/lib/personalTodoStatus";

type Props = {
  todos: ReadonlyArray<Pick<TodoItem, "content" | "status">>;
  title?: string;
  executionStatus?: TodoExecutionStatus;
};

/** One Agent's ordered checklist; statuses never reorder the rows. */
export function PersonalTodoPanel({ todos, title = "任务进度", executionStatus = "idle" }: Props) {
  const [collapsed, setCollapsed] = useState(false);
  if (!todos.length) return null;
  const running = executionStatus === "running";
  const progressLabel = running ? "进行中" : "未完成";
  const notice = {
    running: "", queued: "等待执行", waiting_approval: "等待确认", cancelling: "正在停止执行",
    cancelled: "执行已停止，清单已保留", interrupted: "执行已中断，清单已保留",
    failed: "本轮执行失败，清单已保留", completed: "本轮已结束，清单尚未完成",
    review: "等待核对上次计划", paused: "执行已暂停，清单已保留", idle: "清单已保留，当前未执行",
  }[executionStatus];
  const unfinished = todos.some(todo => todo.status !== "completed");
  const counts = [
    ["completed", "已完成"], ["in_progress", progressLabel], ["pending", "待处理"],
  ].flatMap(([status, label]) => {
    const count = todos.filter(todo => todo.status === status).length;
    return count ? [`${count} ${label}`] : [];
  }).join(" · ");
  return (
    <section className="rounded-2xl border border-line bg-surface px-4 py-3" aria-label={title}>
      <button type="button" className="flex w-full items-center gap-2 text-left text-xs" aria-expanded={!collapsed} onClick={() => setCollapsed(value => !value)}>
        <ListChecks className="size-4 shrink-0 text-ink-muted" />
        <span className="font-medium text-ink">{title}</span>
        <span className="flex-1 text-[10px] text-ink-muted">{counts}</span>
        {collapsed ? <ChevronDown className="size-3.5" /> : <ChevronUp className="size-3.5" />}
      </button>
      {notice && unfinished && <p role="status" className="mt-2 text-[10px] text-ink-muted">{notice}</p>}
      {!collapsed && <ol className="mt-3 max-h-52 space-y-2 overflow-y-auto">
        {todos.map((todo, index) => (
          <li key={`${index}:${todo.content}`} className="flex items-start gap-2 text-xs text-ink-muted" data-status={todo.status}>
            <span className="mt-0.5 shrink-0" aria-label={todo.status === "completed" ? "已完成" : todo.status === "in_progress" ? progressLabel : "待处理"}>
              {todo.status === "completed" ? <Check className="size-3.5 text-success" /> : todo.status === "in_progress" && running ? <Spinner /> : <Circle className="size-3.5 text-ink-faint" />}
            </span>
            <span className="min-w-0 break-words leading-5">{todo.content}</span>
          </li>
        ))}
      </ol>}
    </section>
  );
}
