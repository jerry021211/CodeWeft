import { useState } from "react";
import { Check, ChevronDown, ChevronUp, Circle, ListChecks } from "lucide-react";
import type { TodoItem } from "@/types/api";
import { Spinner } from "@/components/ui";

type Props = {
  todos: ReadonlyArray<Pick<TodoItem, "content" | "status">>;
  title?: string;
};

/** One Agent's ordered checklist; statuses never reorder the rows. */
export function PersonalTodoPanel({ todos, title = "任务进度" }: Props) {
  const [collapsed, setCollapsed] = useState(false);
  if (!todos.length) return null;
  const counts = [
    ["completed", "已完成"], ["in_progress", "进行中"], ["pending", "待处理"],
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
      {!collapsed && <ol className="mt-3 max-h-52 space-y-2 overflow-y-auto">
        {todos.map((todo, index) => (
          <li key={`${index}:${todo.content}`} className="flex items-start gap-2 text-xs text-ink-muted" data-status={todo.status}>
            <span className="mt-0.5 shrink-0" aria-label={todo.status === "completed" ? "已完成" : todo.status === "in_progress" ? "进行中" : "待处理"}>
              {todo.status === "completed" ? <Check className="size-3.5 text-success" /> : todo.status === "in_progress" ? <Spinner /> : <Circle className="size-3.5 text-ink-faint" />}
            </span>
            <span className="min-w-0 break-words leading-5">{todo.content}</span>
          </li>
        ))}
      </ol>}
    </section>
  );
}
