import { Fragment, memo, useEffect, useMemo, useRef, useState } from "react";
import { Bot, CircleStop, Menu, Monitor, Moon, PanelRight, Plug, Sparkles, Sun, Wifi, WifiOff } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Approval, ApprovalDecision, Message } from "@/types/api";
import type { RunViewState } from "@/store/runStore";
import { cx, formatTime, isRunActive, statusLabel } from "@/lib/utils";
import { getRunAnswer } from "@/lib/runAnswer";
import { ThinkingProcess } from "@/components/ThinkingProcess";
import { processAnchors } from "@/lib/conversationProcess";
import { ApprovalBanner } from "@/components/ApprovalBanner";
import { EmptyPanel, IconButton, Spinner, StatusDot } from "@/components/ui";
import { UserQuestions } from "@/components/UserQuestions";
import { ComposerToolbar } from "@/components/ComposerToolbar";
import { AttachmentPicker, type AttachmentPickerHandle } from "@/components/AttachmentPicker";
import type { Attachment } from "@/types/api";

type Props = {
  attachments?: Attachment[];
  onAttachments?: (value: Attachment[]) => void;
  title?: string;
  messages: Message[];
  loading?: boolean;
  run?: RunViewState;
  historyRuns?: Record<string, RunViewState>;
  historyLoading?: boolean;
  historyError?: boolean;
  draft: string;
  sending?: boolean;
  cancelling?: boolean;
  approval?: Approval;
  approvalBusy?: boolean;
  runtimeModel?: string | null;
  teamLeadActive?: boolean;
  teamEnabled?: boolean;
  useTeam?: boolean;
  onTeamChange?: (enabled: boolean) => void;
  readOnly?: boolean;
  webSearch?: boolean;
  webSearchAvailable?: boolean;
  reasoningEffort?: string;
  reasoningOptions?: string[];
  reasoningDefault?: string | null;
  onReasoningChange?: (effort: string) => void;
  onWebSearchChange?: (enabled: boolean) => void;
  onReadOnlyChange: (enabled: boolean) => void;
  workspace?: string;
  theme: "system" | "light" | "dark";
  onDraft: (value: string) => void;
  onSend: () => void;
  onCancel: () => void;
  onApprovalDecision: (decision: ApprovalDecision) => void;
  onOpenLeft: () => void;
  onOpenRight: () => void;
  onOpenMcp: () => void;
  onOpenSettings?: () => void;
  onToggleTheme: () => void;
};

export function ChatWorkspace(props: Props) {
  const attachmentRef = useRef<AttachmentPickerHandle>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const [following, setFollowing] = useState(true);
  const [attachmentsReading, setAttachmentsReading] = useState(false);
  const active = isRunActive(props.run?.status);
  const busy = Boolean(active || props.sending || props.loading);
  const attachmentDisabled = Boolean(busy || props.useTeam || props.teamLeadActive);
  const canSend = Boolean(!busy && !attachmentsReading && (props.draft.trim() || props.attachments?.length));
  const runs = useMemo(() => ({ ...props.historyRuns, ...(props.run ? { [props.run.runId]: props.run } : {}) }), [props.historyRuns, props.run]);
  const anchors = useMemo(() => processAnchors(props.messages, Object.keys(runs)), [props.messages, runs]);
  const process = (id: string) => runs[id] ? <ThinkingProcess key={id} run={runs[id]} /> : null;
  const answer = useMemo(() => props.run ? getRunAnswer(props.run, props.messages) : undefined, [props.run?.runId, props.run?.events, props.run?.status, props.messages]);

  useEffect(() => {
    if (!following) return;
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [props.messages, answer?.text, props.run?.actionOrder.length, following]);

  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  }, [props.draft]);

  const onKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      if (canSend) props.onSend();
    }
  };

  return (
    <main className="flex min-h-0 min-w-0 flex-1 flex-col bg-canvas">
      <header className="flex h-16 shrink-0 items-center gap-3 border-b border-line bg-surface/85 px-3 backdrop-blur-xl sm:px-5">
        <IconButton label="打开会话列表" onClick={props.onOpenLeft} className="lg:hidden"><Menu className="size-4" /></IconButton>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-sm font-semibold text-ink">{props.title || "CodeAgent"}</h1>
          <div className="mt-0.5 flex min-w-0 items-center gap-2 text-[10px] text-ink-muted">
            {props.run ? (
              <>
                <StatusDot status={active ? props.run.status === "waiting_approval" ? "warning" : "running" : props.run.status === "failed" || props.run.status === "interrupted" ? "error" : props.run.status === "completed" ? "success" : "idle"} pulse={active} />
                <span>{statusLabel[props.run.status]}</span>
                {active && <><span aria-hidden>·</span>{props.run.connection === "live" ? <Wifi className="size-3 text-success" /> : props.run.connection === "reconnecting" ? <><Spinner className="size-3" /><span>重连中</span></> : <WifiOff className="size-3" />}</>}
              </>
            ) : (
              <><span className="size-1.5 rounded-full bg-success" /><span className="truncate">{props.runtimeModel || "本地 Agent"}</span></>
            )}
          </div>
        </div>
        {props.workspace && <div className="hidden max-w-52 truncate rounded-lg border border-line bg-surface-muted px-2.5 py-1 font-mono text-[9px] text-ink-muted md:block" title={props.workspace}>{props.workspace}</div>}
        <IconButton label="配置 MCP 插件" onClick={props.onOpenMcp}><Plug className="size-4" /></IconButton>
        <IconButton label={`当前主题：${props.theme === "system" ? "跟随系统" : props.theme === "light" ? "浅色" : "深色"}，点击切换`} onClick={props.onToggleTheme}>
          {props.theme === "system" ? <Monitor className="size-4" /> : props.theme === "light" ? <Sun className="size-4" /> : <Moon className="size-4" />}
        </IconButton>
        <IconButton label="打开运行面板" onClick={props.onOpenRight} className="xl:hidden"><PanelRight className="size-4" /></IconButton>
      </header>

      <div
        ref={scrollRef}
        onScroll={(event) => {
          const target = event.currentTarget;
          setFollowing(target.scrollHeight - target.scrollTop - target.clientHeight < 120);
        }}
        className="scrollbar-thin min-h-0 flex-1 overflow-y-auto"
      >
        {!props.loading && props.messages.length === 0 && !props.run && (
          <div className="grid min-h-full place-items-center">
            <EmptyPanel icon={<Sparkles className="size-5" />} title="有什么可以帮你？" body="提问、讨论、创作或描述你希望完成的工作。" />
          </div>
        )}
        {props.loading && <div className="flex min-h-full items-center justify-center gap-2 text-xs text-ink-muted"><Spinner /> 加载会话…</div>}
        {!props.loading && (props.messages.length > 0 || props.run) && (
          <div className="mx-auto w-full max-w-4xl px-4 py-6 sm:px-7 sm:py-8">
            <div className="space-y-6">
              {props.messages.map((message) => (
                <Fragment key={message.id}>
                  {anchors.before[message.id]?.map(process)}
                  <ChatMessage message={message} />
                  {anchors.after[message.id]?.map(process)}
                </Fragment>
              ))}
              {anchors.trailing.map(process)}
              {props.historyLoading && <div className="text-xs text-ink-muted">加载历史执行记录…</div>}
              {props.historyError && <div className="text-xs text-danger">历史执行记录加载失败，请刷新重试。</div>}
              {answer && (
                <ChatMessage
                  message={{ id: `${props.run?.runId}:stream`, conversation_id: "", role: "assistant", content: answer.text, created_at: props.run?.events.at(-1)?.occurred_at ?? "", status: answer.streaming ? "streaming" : "complete" }}
                />
              )}
              {props.run?.error && <div className="rounded-xl border border-danger/25 bg-danger/5 px-4 py-3 text-xs text-danger">{props.run.error}</div>}
            </div>
          </div>
        )}
      </div>

      <div className="shrink-0 bg-gradient-to-t from-canvas via-canvas to-transparent px-3 pb-3 pt-2 sm:px-5 sm:pb-5">
        {!following && (
          <button type="button" onClick={() => scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" })} className="mx-auto mb-2 block rounded-full border border-line bg-surface px-3 py-1 text-[10px] text-ink-muted shadow-sm hover:text-ink">回到最新消息</button>
        )}
        <div className="mx-auto max-w-4xl space-y-2">
          {props.run && <UserQuestions key={props.run.runId} runId={props.run.runId} active={active && props.run.status !== "cancelling"} />}
          {props.approval && (
            <ApprovalBanner
              approval={props.approval}
              busy={props.approvalBusy}
              onDecision={props.onApprovalDecision}
            />
          )}
          <div className="rounded-2xl border border-line-strong/80 bg-surface shadow-sm transition focus-within:border-accent/40 focus-within:shadow-[0_0_0_3px_rgb(var(--accent)/0.04)]">
          {props.onAttachments && <AttachmentPicker ref={attachmentRef} hideTrigger value={props.attachments ?? []} onChange={props.onAttachments} onBusyChange={setAttachmentsReading} disabled={attachmentDisabled} />}
          <textarea
            ref={inputRef}
            value={props.draft}
            onChange={(event) => props.onDraft(event.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            disabled={busy}
            placeholder={active ? "Agent 正在工作…" : props.teamLeadActive ? "向 Root / Lead 发送团队指令…" : props.useTeam ? "描述团队任务，Lead 将拆分任务并提交方案…" : "告诉 CodeAgent 你想做什么…"}
            aria-label="发送消息"
            className="scrollbar-thin min-h-[76px] w-full resize-none bg-transparent px-4 pb-2 pt-4 text-sm leading-6 text-ink outline-none placeholder:text-ink-faint disabled:cursor-not-allowed disabled:opacity-60"
          />
          <ComposerToolbar busy={busy} active={active} cancelling={Boolean(props.cancelling || props.run?.status === "cancelling")}
            sending={props.sending} canSend={canSend} readOnly={props.readOnly} useTeam={props.useTeam} teamEnabled={props.teamEnabled} teamLeadActive={props.teamLeadActive}
            runtimeModel={props.runtimeModel} reasoningEffort={props.reasoningEffort} reasoningOptions={props.reasoningOptions} reasoningDefault={props.reasoningDefault}
            webSearch={props.webSearch} webSearchAvailable={props.webSearchAvailable} attachmentDisabled={attachmentDisabled} attachmentsReading={attachmentsReading}
            onReadOnlyChange={props.onReadOnlyChange} onTeamChange={props.onTeamChange} onReasoningChange={props.onReasoningChange} onWebSearchChange={props.onWebSearchChange}
            onOpenSettings={props.onOpenSettings} onAttach={props.onAttachments ? () => attachmentRef.current?.open() : undefined} onSend={props.onSend} onCancel={props.onCancel} />
          </div>
        </div>
        <p className="mx-auto mt-2 hidden max-w-4xl px-1 text-right text-[10px] text-ink-faint sm:block">Enter 发送 · Shift + Enter 换行</p>
        {props.run?.status === "cancelling" && <div className="mt-2 flex items-center justify-center gap-1.5 text-[10px] text-ink-muted"><CircleStop className="size-3" /> 取消将在当前安全边界生效</div>}
      </div>
    </main>
  );
}

const ChatMessage = memo(function ChatMessage({ message }: { message: Message }) {
  const user = message.role === "user";
  const attachments = Array.isArray(message.metadata?.attachments) ? message.metadata.attachments as { name: string }[] : [];
  if (message.role === "system") return <div className="mx-auto max-w-lg rounded-full border border-line bg-surface-muted px-3 py-1 text-center text-[10px] text-ink-muted">{message.content}</div>;
  return (
    <article className={cx("flex gap-3", user && "justify-end")}>
      {!user && <div className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-xl border border-accent/20 bg-accent/10 text-accent"><Bot className="size-4" /></div>}
      <div className={cx("min-w-0", user ? "max-w-[88%] rounded-2xl rounded-br-md bg-user-bubble px-4 py-2.5 text-user-bubble-ink shadow-sm sm:max-w-[82%]" : "flex-1")}>
        {user ? (
          <><p className="whitespace-pre-wrap break-words text-sm leading-6">{message.content}</p>
            {attachments.length > 0 && <p className="mt-1 text-xs opacity-70">附件：{attachments.map(item => item.name).join("、")}</p>}</>
        ) : (
          <div className="markdown answer-markdown text-ink"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>{message.status === "streaming" && <span className="ml-1 inline-block h-4 w-0.5 animate-pulse bg-accent align-middle motion-reduce:animate-none" />}</div>
        )}
        <div className={cx("mt-1 text-[9px]", user ? "text-user-bubble-ink/55" : "text-ink-faint")}>{formatTime(message.created_at)}</div>
      </div>
    </article>
  );
});
