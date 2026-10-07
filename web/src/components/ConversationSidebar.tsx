import { useEffect, useId, useMemo, useRef } from "react";
import { Archive, Bot, ChevronRight, Folder, FolderOpen, LoaderCircle, MessageSquare, Plus, Search, Settings2, Trash2, X } from "lucide-react";
import type { Conversation } from "@/types/api";
import { cx, formatRelativeTime, isRunActive, statusLabel } from "@/lib/utils";
import { IconButton, Skeleton, StatusDot } from "@/components/ui";
import { groupConversations, workspaceKey } from "@/lib/conversationGroups";
import { useSidebarStore } from "@/store/sidebarStore";

type Props = {
  conversations: Conversation[];
  selectedId?: string;
  search: string;
  loading?: boolean;
  creating?: boolean;
  creatingWorkspace?: string;
  mobile?: boolean;
  onSearch: (value: string) => void;
  onSelect: (id: string) => void;
  onCreateProject: () => void;
  onCreateConversation: (workspace: string) => void;
  onArchive: (conversation: Conversation) => void;
  onDelete: (conversation: Conversation) => void;
  deletingId?: string;
  onClose?: () => void;
  onOpenSettings?: () => void;
};

export function ConversationSidebar(props: Props) {
  const groups = useMemo(() => groupConversations(props.conversations), [props.conversations]);
  const collapsed = useSidebarStore((state) => state.collapsed);
  const setCollapsed = useSidebarStore((state) => state.setCollapsed);
  const listId = useId();
  const searching = Boolean(props.search.trim());
  const selected = props.conversations.find((item) => item.id === props.selectedId);
  const selectedWorkspace = selected ? workspaceKey(selected.workspace) : undefined;
  const revealedSelection = useRef<string>();
  useEffect(() => {
    if (!searching && selectedWorkspace !== undefined && revealedSelection.current !== props.selectedId) {
      setCollapsed(selectedWorkspace, false);
      revealedSelection.current = props.selectedId;
    }
  }, [props.selectedId, selectedWorkspace, searching, setCollapsed]);
  return (
    <aside className="flex h-full min-h-0 w-full flex-col bg-sidebar text-sidebar-ink">
      <header className="flex h-16 shrink-0 items-center justify-between px-5">
        <div className="flex min-w-0 items-center gap-2.5">
          <div className="grid size-8 shrink-0 place-items-center rounded-lg border border-accent/20 bg-accent/10 text-accent">
            <Bot className="size-4" />
          </div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold tracking-tight">CodeWeft</div>
            <div className="mt-0.5 text-[10px] text-sidebar-muted/70">本地工作台</div>
          </div>
        </div>
        {props.mobile && props.onClose && (
          <IconButton label="关闭会话栏" onClick={props.onClose} className="text-sidebar-muted hover:bg-white/5 hover:text-white">
            <X className="size-4" />
          </IconButton>
        )}
      </header>

      <div className="space-y-2 px-3 pb-4">
        <button
          type="button"
          onClick={props.onCreateProject}
          disabled={props.creating}
          className="flex h-9 w-full items-center gap-2 rounded-lg border border-accent/25 bg-accent/10 px-3 text-xs font-medium text-sidebar-ink transition hover:border-accent/40 hover:bg-accent/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          <Plus className="size-3.5 text-accent" /> 新建项目
        </button>
        <label className="flex h-8 items-center gap-2 rounded-lg border border-transparent bg-white/[0.025] px-3 text-sidebar-muted transition focus-within:border-white/10 focus-within:bg-white/[0.05]">
          <Search className="size-3.5 shrink-0" />
          <input
            value={props.search}
            onChange={(event) => props.onSearch(event.target.value)}
            placeholder="搜索项目或会话"
            aria-label="搜索会话"
            className="min-w-0 flex-1 bg-transparent text-xs text-sidebar-ink outline-none placeholder:text-sidebar-muted/70"
          />
        </label>
      </div>

      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex items-center gap-2 px-5 pb-2 text-[10px] font-medium text-sidebar-muted"><span>{searching ? "搜索结果" : "项目"}</span><span className="text-sidebar-muted/50">{groups.length}</span></div>
        <nav className="scrollbar-thin flex-1 space-y-1 overflow-y-auto px-3 pb-4" aria-label="会话列表">
          {props.loading && Array.from({ length: 5 }, (_, index) => <Skeleton key={index} className="mb-1 h-14 bg-white/[0.05]" />)}
          {!props.loading && props.conversations.length === 0 && (
            <div className="mx-2 mt-5 rounded-xl border border-dashed border-white/10 px-4 py-6 text-center text-xs leading-5 text-sidebar-muted">
              <MessageSquare className="mx-auto mb-2 size-5 opacity-70" />
              {props.search ? "没有匹配的会话" : "还没有项目，选择目录开始工作"}
            </div>
          )}
          {groups.map((group, index) => {
            const expanded = searching || !collapsed.includes(group.key);
            const runningCount = group.conversations.filter((item) => isRunActive(item.run_status)).length;
            const containsSelected = group.conversations.some((item) => item.id === props.selectedId);
            const duplicateName = groups.some((item) => item.key !== group.key && item.name.toLocaleLowerCase() === group.name.toLocaleLowerCase());
            const FolderIcon = expanded ? FolderOpen : Folder;
            const groupId = `${listId}-project-${index}`;
            const creatingHere = props.creating && props.creatingWorkspace != null && workspaceKey(props.creatingWorkspace) === group.key;
            return <section key={group.key} className={cx("rounded-xl", expanded && "bg-white/[0.02]")}>
              <div className="flex items-center gap-0.5 pr-1.5">
              <button type="button" aria-expanded={expanded} aria-controls={groupId}
                aria-label={`${expanded ? "收起" : "展开"}项目 ${group.name}`}
                disabled={searching}
                title={searching ? `${group.workspace}\n搜索时自动展开匹配项目` : group.workspace}
                onClick={() => setCollapsed(group.key, expanded)}
                className={cx("flex min-h-10 min-w-0 flex-1 items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition hover:bg-white/[0.04] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent", containsSelected ? "text-white" : "text-sidebar-ink/80")}>
                <FolderIcon className={cx("size-3.5 shrink-0", containsSelected ? "text-accent" : "text-sidebar-muted/70")} />
                <span className="min-w-0 flex-1"><span className="block truncate text-xs font-medium">{group.name}</span>{duplicateName && <span className="mt-0.5 block truncate text-[9px] text-sidebar-muted/60">{group.workspace}</span>}</span>
                {runningCount > 0 && <span title={`${runningCount} 个会话运行中`} aria-label={`${runningCount} 个会话运行中`}><StatusDot status="running" pulse /></span>}
                <span className="min-w-3 text-right text-[10px] tabular-nums text-sidebar-muted/60">{group.conversations.length}</span>
                <ChevronRight className={cx("size-3 shrink-0 text-sidebar-muted/60 transition-transform", expanded && "rotate-90")} />
              </button>
              <button type="button" aria-label={`在项目 ${group.name} 中新建会话`} title={`新建会话 · ${group.workspace}`}
                disabled={props.creating || !group.workspace}
                onClick={() => props.onCreateConversation(group.workspace)}
                className="grid size-7 shrink-0 place-items-center rounded-md text-sidebar-muted/70 transition hover:bg-accent/10 hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40">
                {creatingHere ? <LoaderCircle className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />}
              </button>
              </div>
              <div id={groupId} hidden={!expanded} className="ml-4 mr-1 space-y-0.5 border-l border-white/[0.08] pb-1.5 pl-2">
          {group.conversations.map((conversation) => {
            const active = props.selectedId === conversation.id;
            const running = isRunActive(conversation.run_status);
            return (
              <div
                key={conversation.id}
                className={cx(
                  "group relative flex rounded-lg transition",
                  active ? "bg-accent/[0.12] text-white" : "text-sidebar-ink/80 hover:bg-white/[0.04]",
                )}
              >
                <button
                  type="button"
                  aria-current={active ? "page" : undefined}
                  onClick={() => props.onSelect(conversation.id)}
                  className="min-w-0 flex-1 px-2.5 py-2.5 text-left focus-visible:rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                >
                  {active && <span aria-hidden="true" className="absolute inset-y-3 left-0 w-0.5 rounded-full bg-accent" />}
                  <span className="flex items-center gap-2">
                    <span className="truncate text-xs font-medium">{conversation.title || "新会话"}</span>
                    {(running || conversation.run_status === "failed") && (
                      <StatusDot
                        status={running ? (conversation.waiting_for_answer || conversation.run_status === "waiting_approval" ? "warning" : "running") : conversation.run_status === "failed" ? "error" : "success"}
                        pulse={running}
                      />
                    )}
                    {conversation.waiting_for_answer && <span className="shrink-0 text-[10px] text-amber-400">待回答</span>}
                  </span>
                  <span className={cx("mt-1 flex items-center justify-between gap-2 text-[10px] text-sidebar-muted/70 group-hover:pr-14 group-focus-within:pr-14", props.mobile && "pr-14")}>
                    <span className="truncate">{conversation.last_message || (conversation.run_status ? statusLabel[conversation.run_status] : "等待消息")}</span>
                    <span className={cx("shrink-0 text-[9px] group-hover:hidden group-focus-within:hidden", props.mobile && "hidden")}>{formatRelativeTime(conversation.updated_at)}</span>
                  </span>
                </button>
                <div className={cx("absolute bottom-1.5 right-1.5 flex gap-0.5 rounded-md bg-sidebar transition group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100", props.mobile ? "opacity-100" : "pointer-events-none opacity-0")}>
                  <button
                    type="button"
                    aria-label={`归档 ${conversation.title}`}
                    title="归档"
                    onClick={() => props.onArchive(conversation)}
                    disabled={props.deletingId === conversation.id}
                    className="grid size-6 place-items-center rounded-md text-sidebar-muted transition hover:bg-white/5 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-40"
                  >
                    <Archive className="size-3" />
                  </button>
                  <button
                    type="button"
                    aria-label={`删除 ${conversation.title}`}
                    title={running ? "请先停止任务再删除" : "删除会话"}
                    disabled={running || Boolean(props.deletingId)}
                    onClick={() => props.onDelete(conversation)}
                    className="grid size-6 place-items-center rounded-md text-sidebar-muted transition hover:bg-danger/10 hover:text-danger focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <Trash2 className="size-3" />
                  </button>
                </div>
              </div>
            );
          })}
              </div>
            </section>;
          })}
        </nav>
      </div>
      <footer className="shrink-0 border-t border-white/[0.06] px-3 py-3 text-[10px] text-sidebar-muted">
        <button type="button" onClick={props.onOpenSettings} className="mb-2 flex h-9 w-full items-center gap-2 rounded-lg px-3 text-xs text-sidebar-ink transition hover:bg-white/5"><Settings2 className="size-4" />设置</button>
        <span className="px-3">数据仅保存在本机</span>
      </footer>
    </aside>
  );
}
