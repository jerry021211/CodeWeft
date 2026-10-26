import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Clock, Plus } from "lucide-react";
import { api } from "@/lib/api";
import { intervalLabel, localScheduleTime, scheduleLabel, scheduleTimeToISO } from "@/lib/schedules";
import type { Conversation, ScheduledTask, ScheduledTaskInput } from "@/types/api";
import { EmptyPanel, Spinner } from "@/components/ui";

const button = "rounded-lg border border-line px-3 py-2 text-xs transition hover:bg-surface-muted disabled:opacity-40";
const field = "w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm outline-none focus:border-accent";
const schedulesKey = ["schedules"] as const;
const dateLabel = (value: string) => new Date(value).toLocaleString();

export function SchedulesPanel({ onOpen }: { onOpen: (conversation: Conversation) => void }) {
  const client = useQueryClient();
  const [editor, setEditor] = useState<ScheduledTask | "new">();
  const [historyId, setHistoryId] = useState<string>();
  const query = useQuery({ queryKey: schedulesKey, queryFn: api.listSchedules, refetchInterval: 5000 });
  const runtime = useQuery({ queryKey: ["runtime-config"], queryFn: api.getRuntimeConfig });
  const conversations = useQuery({ queryKey: ["conversations"], queryFn: () => api.listConversations({ archived: false }) });
  const workspaces = [...new Set([runtime.data?.workspace, ...(conversations.data ?? []).map(c => c.workspace)].filter((x): x is string => Boolean(x)))];
  const history = useQuery({
    queryKey: ["schedule-history", historyId], queryFn: () => api.scheduleHistory(historyId!),
    enabled: Boolean(historyId), refetchInterval: 3000,
  });
  const save = useMutation({
    mutationFn: ({ input, id }: { input: ScheduledTaskInput; id?: string }) => api.saveSchedule(input, id),
    onSuccess: () => { setEditor(undefined); void client.invalidateQueries({ queryKey: schedulesKey }); },
  });
  const action = useMutation({
    mutationFn: async ({ task, remove }: { task: ScheduledTask; remove?: boolean }) => {
      if (remove) await api.deleteSchedule(task.id);
      else await api.setScheduleEnabled(task.id, !task.enabled);
    },
    onSuccess: (_, { task, remove }) => {
      if (remove && historyId === task.id) setHistoryId(undefined);
      if (remove && editor !== "new" && editor?.id === task.id) setEditor(undefined);
      void client.invalidateQueries({ queryKey: schedulesKey });
    },
  });
  const open = useMutation({
    mutationFn: async (id: string) => {
      let conversation = await api.getConversation(id);
      if (conversation.archived_at) conversation = await api.updateConversation(id, { archived: false });
      onOpen(conversation);
    },
  });
  const busy = save.isPending || action.isPending;
  const error = query.error || action.error || open.error;
  return <div className="space-y-5">
    <p className="rounded-xl border border-line bg-surface-muted p-4 text-sm leading-6 text-ink-muted">保持 CodeWeft 后端运行即可自动执行，浏览器可以关闭。每次触发创建独立会话；服务离线期间错过的周期合并为一次，上次任务未结束时跳过本次。</p>
    {error && <p role="alert" className="text-sm text-danger">{error.message}</p>}
    <button type="button" className={`${button} inline-flex items-center gap-2`} disabled={busy} onClick={() => { save.reset(); setEditor("new"); }}><Plus className="size-4" />新建定时任务</button>
    {editor && <ScheduleEditor key={editor === "new" ? "new" : editor.id} task={editor === "new" ? undefined : editor}
      workspaces={workspaces} busy={save.isPending} error={save.error?.message}
      onCancel={() => setEditor(undefined)} onSave={input => save.mutate({ input, id: editor === "new" ? undefined : editor.id })} />}
    {query.isPending ? <div role="status" className="flex items-center gap-2"><Spinner />正在读取定时任务…</div>
      : query.data?.length === 0 ? <EmptyPanel icon={<Clock className="size-5" />} title="暂无定时任务" body="安排一次代码检查，或定期生成项目进展报告。" />
        : query.data?.map(task => <article key={task.id} className="space-y-3 rounded-xl border border-line bg-surface p-4">
          <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="break-all text-sm font-semibold">{task.name}</h2><span className="text-xs text-ink-muted">{scheduleLabel(task)}</span></div>
          <p className="break-all text-xs text-ink-muted">{task.workspace}</p>
          <p className="whitespace-pre-wrap break-words text-sm line-clamp-3">{task.prompt}</p>
          <p className="text-xs leading-6 text-ink-muted">{task.kind === "once" ? "执行一次" : intervalLabel(task.interval_seconds!)} · {task.read_only ? "只读" : "Agent"}<br />{task.enabled && task.next_run_at ? `下次执行：${dateLabel(task.next_run_at)}` : "暂无待执行时间"}</p>
          <div className="flex flex-wrap gap-2">
            <button type="button" className={button} disabled={busy} onClick={() => { save.reset(); setEditor(task); }}>编辑</button>
            <button type="button" className={button} disabled={busy || (!task.enabled && task.kind === "once" && new Date(task.run_at).getTime() <= Date.now())} onClick={() => action.mutate({ task })}>{task.enabled ? "暂停" : "启用"}</button>
            <button type="button" className={button} aria-expanded={historyId === task.id} onClick={() => setHistoryId(historyId === task.id ? undefined : task.id)}>执行记录</button>
            <button type="button" className={`${button} text-danger`} disabled={busy} onClick={() => {
              if (window.confirm(`删除定时任务「${task.name}」及其触发记录？已生成的会话会保留，正在执行的任务不会被取消。`)) action.mutate({ task, remove: true });
            }}>删除</button>
          </div>
          {historyId === task.id && <div className="space-y-3 border-t border-line pt-3">
            {history.isPending && <Spinner />}
            {history.error && <p role="alert" className="text-xs text-danger">{history.error.message}</p>}
            {history.data?.length === 0 && <p className="text-xs text-ink-muted">尚未触发。</p>}
            {history.data?.map(item => <div key={item.id} className="text-xs leading-6">
              <div className="flex flex-wrap items-center justify-between gap-2"><span>{dateLabel(item.claimed_at)} · {statusLabel(item.run_status ?? item.status)}</span>
                {item.conversation_id && <button type="button" className={button} disabled={open.isPending} onClick={() => open.mutate(item.conversation_id!)}>查看会话</button>}</div>
              <p className="text-ink-faint">计划时间：{dateLabel(item.scheduled_at)}</p>
              {item.error && <p className="break-words text-danger">{item.error}</p>}
            </div>)}
          </div>}
        </article>)}
  </div>;
}

function ScheduleEditor({ task, workspaces, busy, error, onSave, onCancel }: {
  task?: ScheduledTask; workspaces: string[]; busy: boolean; error?: string;
  onSave: (input: ScheduledTaskInput) => void; onCancel: () => void;
}) {
  const [name, setName] = useState(task?.name ?? "");
  const [workspace, setWorkspace] = useState(task?.workspace ?? workspaces[0] ?? "");
  const [prompt, setPrompt] = useState(task?.prompt ?? "");
  const [kind, setKind] = useState<"once" | "interval">(task?.kind ?? "once");
  const [runAt, setRunAt] = useState(localScheduleTime(task?.next_run_at ?? new Date(Date.now() + 3600_000).toISOString()));
  const [seconds, setSeconds] = useState(task?.interval_seconds ?? 3600);
  const [readOnly, setReadOnly] = useState(task?.read_only ?? true);
  const [enabled, setEnabled] = useState(task?.enabled ?? true);
  const [localError, setLocalError] = useState("");
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return <form className="space-y-4 rounded-xl border border-accent/30 bg-surface p-5" onSubmit={event => {
    event.preventDefault(); setLocalError("");
    try {
      const iso = scheduleTimeToISO(runAt);
      if (enabled && new Date(iso).getTime() <= Date.now()) throw new Error("首次执行时间必须晚于当前时间。");
      onSave({ name: name.trim(), workspace: workspace.trim(), prompt: prompt.trim(), kind, run_at: iso,
        interval_seconds: kind === "interval" ? seconds : null, read_only: readOnly, enabled });
    } catch (cause) { setLocalError(cause instanceof Error ? cause.message : "请输入有效的时间。"); }
  }}>
    <h2 className="text-sm font-semibold">{task ? "编辑定时任务" : "新建定时任务"}</h2>
    <fieldset disabled={busy} className="space-y-4">
      <label className="block space-y-2 text-xs">任务名称<input required maxLength={200} className={field} value={name} onChange={e => setName(e.target.value)} placeholder="例如：每日代码检查" /></label>
      <label className="block space-y-2 text-xs">项目目录<input required list="schedule-workspaces" className={field} value={workspace} onChange={e => setWorkspace(e.target.value)} placeholder="已有项目的绝对路径" /><datalist id="schedule-workspaces">{workspaces.map(path => <option key={path} value={path} />)}</datalist></label>
      <label className="block space-y-2 text-xs">任务提示词<textarea required maxLength={200000} rows={4} className={field} value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="描述每次执行时需要完成的工作…" /></label>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="block space-y-2 text-xs">执行方式<select className={field} value={kind} onChange={e => setKind(e.target.value as "once" | "interval")}><option value="once">指定时间执行一次</option><option value="interval">固定间隔重复</option></select></label>
        <label className="block space-y-2 text-xs">首次执行时间（{zone}）<input required type="datetime-local" className={field} value={runAt} onChange={e => setRunAt(e.target.value)} /></label>
      </div>
      {kind === "interval" && <label className="block space-y-2 text-xs">重复间隔（秒，至少 60；3600 = 1 小时，86400 = 1 天）<input required type="number" min={60} max={31536000} step={1} className={field} value={seconds} onChange={e => setSeconds(Number(e.target.value))} /></label>}
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={readOnly} onChange={e => setReadOnly(e.target.checked)} />只读执行</label>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />启用任务</label>
      <p className="text-xs leading-5 text-ink-muted">定时执行使用当前模型配置和现有工具权限。需要审批的操作仍需在会话中确认。固定间隔按实际经过时间计算。</p>
    </fieldset>
    {(localError || error) && <p role="alert" className="text-xs text-danger">{localError || error}</p>}
    <div className="flex gap-2"><button type="submit" disabled={busy || !name.trim() || !prompt.trim() || !workspace.trim()} className={`${button} bg-accent text-white`}>{busy ? "保存中…" : "保存任务"}</button><button type="button" className={button} disabled={busy} onClick={onCancel}>取消</button></div>
  </form>;
}

function statusLabel(status: string) {
  return ({ dispatching: "提交中", submitted: "已提交", skipped: "已跳过", failed: "失败", interrupted: "已中断",
    queued: "排队中", running: "执行中", completed: "已完成", cancelled: "已取消" } as Record<string, string>)[status] ?? status;
}
