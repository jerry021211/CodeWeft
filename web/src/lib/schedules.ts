import type { ScheduledTask } from "@/types/api";

// datetime-local uses the browser's timezone; API values always include an offset.
export function localScheduleTime(iso: string): string {
  const date = new Date(iso);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

export function scheduleTimeToISO(local: string): string {
  const date = new Date(local);
  if (!local || !Number.isFinite(date.getTime()) || localScheduleTime(date.toISOString()) !== local) {
    throw new Error("请选择有效的执行时间；夏令时跳过的时间不可用。");
  }
  return date.toISOString();
}

export function scheduleLabel(task: ScheduledTask): string {
  if (!task.enabled) return task.kind === "once" && !task.next_run_at ? "未安排下一次" : "已暂停";
  return "已启用";
}

export function intervalLabel(seconds: number): string {
  if (seconds % 86400 === 0) return `每 ${seconds / 86400} 天`;
  if (seconds % 3600 === 0) return `每 ${seconds / 3600} 小时`;
  if (seconds % 60 === 0) return `每 ${seconds / 60} 分钟`;
  return `每 ${seconds} 秒`;
}
