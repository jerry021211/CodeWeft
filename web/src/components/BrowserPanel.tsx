import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Globe, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import type { BrowserSession } from "@/types/api";

export function browserPageLink(value: string): string | undefined {
  try {
    const url = new URL(value);
    if (["http:", "https:"].includes(url.protocol) && !url.username && !url.password) return url.href;
  } catch { /* A non-web page has no external-open action. */ }
}

const labels = { agent: "Agent 控制", pausing: "等待当前操作结束", human: "由你控制", resuming: "正在重新读取页面", error: "需要处理", closing: "正在关闭", closed: "已关闭" };

export function BrowserSessionCard({ session, busy, onAction }: {
  session: BrowserSession; busy: boolean; onAction: (action: "takeover" | "handback" | "close") => void;
}) {
  const url = browserPageLink(session.url);
  const closed = session.control === "closed" || session.control === "closing";
  return <section className="space-y-3 rounded-2xl border border-line bg-surface-muted p-3.5">
    <div className="flex items-center justify-between gap-2 text-xs"><strong className="truncate">{session.server}</strong><span className="shrink-0 text-ink-muted">{session.started ? labels[session.control] : closed ? labels[session.control] : "等待打开页面"}</span></div>
    {session.title && <p className="break-words text-sm font-medium">{session.title}</p>}
    {session.url && <p className="break-all text-xs text-ink-muted">{session.url}</p>}
    {session.updated_at && <p className="text-[10px] text-ink-faint">最近确认：{new Date(session.updated_at * 1000).toLocaleTimeString()} · 页面操作后更新</p>}
    {session.control === "human" && <p className="text-xs leading-5 text-ink-muted">请切换到服务所在电脑上的独立浏览器窗口操作。此时 Agent 不会操作浏览器，页面信息暂不自动更新。</p>}
    {session.control === "pausing" && <p role="status" className="text-xs leading-5 text-warning">正在等待在途操作结束，请先不要操作窗口。状态变为“由你控制”后再开始。</p>}
    {session.headless && <p className="text-xs text-ink-faint">后台浏览器不支持人工接管；需要登录时请使用可见预设。</p>}
    {session.error && <p role="status" className="text-xs leading-5 text-warning">{session.error}</p>}
    <div className="flex flex-wrap gap-2 text-xs">
      {!closed && <>
        {session.control === "human" ? <button type="button" disabled={busy} onClick={() => onAction("handback")} className="rounded-lg bg-accent px-3 py-2 text-white disabled:opacity-50">交还 Agent</button>
          : <button type="button" disabled={busy || session.headless || !session.started || session.control !== "agent"} onClick={() => onAction("takeover")} className="rounded-lg bg-accent px-3 py-2 text-white disabled:opacity-50">接管浏览器</button>}
        <button type="button" disabled={busy} onClick={() => onAction("close")} className="rounded-lg border border-line px-3 py-2 disabled:opacity-50">关闭浏览器</button>
      </>}
      {url && <a href={url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 rounded-lg border border-line px-3 py-2" title="打开地址，不复制受控浏览器的登录状态"><ExternalLink className="size-3" />外部打开</a>}
    </div>
  </section>;
}

export function BrowserPanel({ conversationId }: { conversationId?: string }) {
  const client = useQueryClient();
  const key = ["browsers", conversationId];
  const query = useQuery({ queryKey: key, queryFn: () => api.browserSessions(conversationId!), enabled: Boolean(conversationId), refetchInterval: 2000 });
  const action = useMutation({
    mutationFn: ({ id, command }: { id: string; command: "takeover" | "handback" | "close" }) => api.browserAction(conversationId!, id, command),
    onSuccess: (session) => client.setQueryData<BrowserSession[]>(key, current => current?.map(item => item.id === session.id ? session : item)),
    onSettled: () => { void client.invalidateQueries({ queryKey: key }); },
  });
  return <div className="space-y-4 p-4">
    <div className="flex items-center justify-between text-sm font-medium"><span className="inline-flex items-center gap-2"><Globe className="size-4" />当前会话浏览器</span><button type="button" aria-label="刷新浏览器状态" disabled={!conversationId || query.isFetching} onClick={() => void query.refetch()}><RefreshCw className="size-3.5" /></button></div>
    {(query.error || action.error) && <p role="alert" className="text-xs text-danger">{(action.error || query.error)?.message}</p>}
    {query.isLoading && conversationId && <p className="text-xs text-ink-muted">正在读取状态…</p>}
    {!query.isLoading && !query.data?.length && <p className="text-xs leading-6 text-ink-muted">尚未启动会话浏览器。在 MCP 设置中启用并信任浏览器预设，再让 Agent 打开页面。</p>}
    {query.data?.map(session => <BrowserSessionCard key={session.id} session={session} busy={action.isPending}
      onAction={command => action.mutate({ id: session.id, command })} />)}
    <p className="text-xs leading-5 text-ink-faint">页面和临时登录状态在本会话的多轮对话间保留。闲置 30 分钟、取消执行或关闭后清理，后端重启不恢复。交还后若本轮已结束，发送消息让 Agent 继续。</p>
  </div>;
}
