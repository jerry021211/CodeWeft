import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, ArchiveRestore, ArrowLeft, Eye, Folder, RefreshCw, Search, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { groupConversations, workspaceKey } from "@/lib/conversationGroups";
import { isRunActive } from "@/lib/utils";
import type { Conversation } from "@/types/api";
import { ChatMessage } from "@/components/ChatWorkspace";
import { EmptyPanel, IconButton, Spinner } from "@/components/ui";

export const archivedConversationsKey = ["archived-conversations"] as const;
const buttonClass = "inline-flex items-center justify-center gap-2 rounded-lg border border-line px-3 py-2 text-xs text-ink-muted transition hover:bg-surface-muted disabled:opacity-40";

export function ArchivedConversationsPanel({ onRestored, onDelete }: {
  onRestored: (conversation: Conversation, open: boolean) => void;
  onDelete: (conversation: Conversation) => Promise<unknown>;
}) {
  const client = useQueryClient();
  const [search, setSearch] = useState("");
  const [project, setProject] = useState("");
  const [selected, setSelected] = useState<Conversation>();
  const [notice, setNotice] = useState("");
  const archived = useQuery({ queryKey: archivedConversationsKey, queryFn: ({ signal }) => api.listArchivedConversations(signal) });
  const groups = useMemo(() => groupConversations(archived.data ?? []), [archived.data]);
  useEffect(() => {
    if (archived.isSuccess && project && !groups.some(group => group.key === project)) setProject("");
  }, [archived.isSuccess, groups, project]);
  const filtered = useMemo(() => {
    const term = search.trim().toLocaleLowerCase();
    return (archived.data ?? []).filter(item => (!project || workspaceKey(item.workspace) === project)
      && `${item.title} ${item.workspace} ${item.last_message ?? ""}`.toLocaleLowerCase().includes(term));
  }, [archived.data, search, project]);
  const messages = useQuery({
    queryKey: ["conversations", selected?.id, "messages"],
    queryFn: () => api.listMessages(selected!.id), enabled: Boolean(selected),
  });
  const action = useMutation({
    mutationFn: async ({ conversation, kind, open = false }: { conversation: Conversation; kind: "restore" | "delete"; open?: boolean }) => {
      setNotice("");
      if (kind === "delete") { await onDelete(conversation); return; }
      const restored = await api.updateConversation(conversation.id, { archived: false });
      await client.cancelQueries({ queryKey: ["conversations"], exact: true });
      onRestored(restored, open);
    },
    onSuccess: async (_, { conversation, kind }) => {
      await client.cancelQueries({ queryKey: archivedConversationsKey });
      client.setQueryData<Conversation[]>(archivedConversationsKey, current => current?.filter(item => item.id !== conversation.id));
      if (selected?.id === conversation.id) setSelected(undefined);
      setNotice(kind === "restore" ? "会话已恢复，可在原项目下继续对话。" : "会话已永久删除，工作区文件已保留。");
      void client.invalidateQueries({ queryKey: archivedConversationsKey });
    },
  });
  const remove = (conversation: Conversation) => {
    if (window.confirm(`确定永久删除会话「${conversation.title || "新会话"}」？\n聊天记录和运行记录将被删除，无法恢复。工作区文件会保留。`)) {
      action.mutate({ conversation, kind: "delete" });
    }
  };
  const actions = (conversation: Conversation, preview = false) => <div className="flex shrink-0 items-center justify-end gap-1">
    {!preview && <IconButton label={`查看 ${conversation.title}`} disabled={action.isPending} onClick={() => { setSelected(conversation); action.reset(); setNotice(""); }}><Eye className="size-4" /></IconButton>}
    <button type="button" disabled={action.isPending} className={buttonClass} onClick={() => action.mutate({ conversation, kind: "restore", open: preview })}>
      {action.isPending && action.variables?.conversation.id === conversation.id ? <Spinner /> : <ArchiveRestore className="size-4" />}{preview ? "恢复并打开" : "恢复"}
    </button>
    <IconButton label={`永久删除 ${conversation.title}`} className="hover:text-danger" disabled={action.isPending || isRunActive(conversation.run_status)} onClick={() => remove(conversation)}><Trash2 className="size-4" /></IconButton>
  </div>;

  return <div className="space-y-5">
    {action.isError && <p role="alert" className="rounded-xl border border-danger/20 bg-danger/5 p-3 text-sm text-danger">{action.error.message}</p>}
    {notice && <p role="status" className="rounded-xl border border-success/20 bg-success/5 p-3 text-sm text-success">{notice}</p>}
    {selected ? <>
      <button type="button" className={buttonClass} onClick={() => setSelected(undefined)}><ArrowLeft className="size-4" />返回归档列表</button>
      <div className="rounded-2xl border border-line bg-surface">
        <div className="flex flex-wrap items-center justify-between gap-4 border-b border-line p-5">
          <div className="min-w-0 flex-1"><h2 className="break-words text-base font-semibold">{selected.title || "新会话"}</h2><p className="mt-2 break-all text-xs text-ink-muted">{selected.workspace}</p><p className="mt-1 text-xs text-ink-faint">归档于 {dateLabel(selected.archived_at)} · 历史预览</p></div>
          {actions(selected, true)}
        </div>
        <div className="space-y-6 p-4 sm:p-6">
          {messages.isPending && <div role="status" className="flex items-center gap-2 text-sm text-ink-muted"><Spinner />正在读取聊天记录…</div>}
          {messages.isError && <div role="alert" className="text-sm text-danger">{messages.error.message}<button type="button" className={`${buttonClass} ml-3`} onClick={() => void messages.refetch()}>重试</button></div>}
          {messages.data?.map(message => <ChatMessage key={message.id} message={message} />)}
          {messages.data?.length === 0 && <p className="py-10 text-center text-sm text-ink-muted">此会话暂无聊天记录。</p>}
        </div>
      </div>
    </> : <>
      <div className="flex flex-col gap-3 sm:flex-row">
        <label className="flex h-10 min-w-0 items-center gap-2 rounded-xl border border-line bg-surface px-3 focus-within:border-accent sm:flex-1"><Search className="size-4 shrink-0 text-ink-faint" /><input aria-label="搜索归档会话" value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索会话或项目…" className="min-w-0 flex-1 bg-transparent text-sm outline-none" /></label>
        <select aria-label="筛选归档项目" value={project} onChange={event => setProject(event.target.value)} className="h-10 max-w-full rounded-xl border border-line bg-surface px-3 text-sm sm:max-w-64"><option value="">全部项目</option>{groups.map(group => <option key={group.key} value={group.key}>{group.name} · {group.workspace}</option>)}</select>
        <IconButton label="刷新归档会话" disabled={archived.isFetching} onClick={() => void archived.refetch()}><RefreshCw className="size-4" /></IconButton>
      </div>
      {archived.isPending ? <div role="status" className="flex items-center justify-center gap-2 py-16 text-sm text-ink-muted"><Spinner />正在读取归档会话…</div>
        : archived.isError ? <div role="alert" className="rounded-xl border border-danger/20 p-4 text-sm text-danger">{archived.error.message}<button type="button" className={`${buttonClass} ml-3`} onClick={() => void archived.refetch()}>重试</button></div>
        : <>
          <p className="text-xs text-ink-faint">{filtered.length} 个归档会话{search || project ? ` / 共 ${archived.data.length} 个` : ""}</p>
          {filtered.length === 0 ? <EmptyPanel icon={<Archive className="size-5" />} title={archived.data.length ? "没有匹配的归档会话" : "暂无归档会话"} body={archived.data.length ? "试试其他关键词，或选择全部项目。" : "侧栏中归档的会话会出现在这里，聊天记录仍会保留。"} />
            : <div className="divide-y divide-line overflow-hidden rounded-2xl border border-line bg-surface">{filtered.map(conversation => <article key={conversation.id} className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:p-5">
              <div className="hidden size-10 shrink-0 place-items-center rounded-xl bg-surface-muted text-ink-muted sm:grid"><Archive className="size-4" /></div>
              <button type="button" disabled={action.isPending} className="min-w-0 flex-1 text-left" onClick={() => { setSelected(conversation); action.reset(); setNotice(""); }}>
                <span className="block truncate text-sm font-medium hover:text-accent">{conversation.title || "新会话"}</span>
                <span className="mt-1.5 flex items-center gap-1.5 text-xs text-ink-muted"><Folder className="size-3 shrink-0" /><span className="truncate" title={conversation.workspace}>{conversation.workspace.split(/[\\/]+/).filter(Boolean).at(-1) || "未绑定工作区"} · {conversation.workspace}</span></span>
                <span className="mt-1.5 block text-xs text-ink-faint">归档于 {dateLabel(conversation.archived_at)}</span>
              </button>
              {actions(conversation)}
            </article>)}</div>}
        </>}
    </>}
  </div>;
}

function dateLabel(value?: string | null) {
  return value ? new Date(value).toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "时间未知";
}
