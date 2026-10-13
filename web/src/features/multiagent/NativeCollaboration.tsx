import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Bot, CheckCircle2, ChevronDown, CircleAlert, Expand, GitBranch, LoaderCircle, Minus, Plus, RotateCcw, Users, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { RunViewState } from "@/store/runStore";
import { asRecord, asString, cx, formatDuration, isRunActive } from "@/lib/utils";
import { buildProcessPresentation } from "@/lib/processPresentation";
import { ProcessStep } from "@/components/ThinkingProcess";
import { api } from "@/lib/api";
import { collaborationLabels as labels, collaborationLayout, collaborationTasks, dependencies, GRAPH_CARD_HEIGHT, GRAPH_CARD_WIDTH,
  memberPhase, memberRoles, memberTrace, nativeSnapshots, resolveCollaboration, type CollaborationSnapshot, type CollaborationSync } from "@/features/multiagent/presentation";

export { nativeSnapshots } from "@/features/multiagent/presentation";
const focus = "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60";

type Props = {run: RunViewState; relatedRuns?: Record<string, RunViewState>; sync?: CollaborationSync; onContinue?: (text: string) => void};

export function NativeCollaboration(props: Props) {
  return <>{nativeSnapshots(props.run).map(snapshot => <CollaborationCard key={String(snapshot.id)} {...props}
    {...resolveCollaboration(props.run, String(snapshot.id), props.relatedRuns, props.sync?.snapshots)} />)}</>;
}

function StatusIcon({phase}: {phase: string}) {
  if (["running", "verifying", "delivering"].includes(phase)) return <LoaderCircle aria-hidden className="size-4 shrink-0 animate-spin text-accent motion-reduce:animate-none" />;
  if (["accepted", "completed"].includes(phase)) return <CheckCircle2 aria-hidden className="size-4 shrink-0 text-emerald-600" />;
  if (["blocked", "failed"].includes(phase)) return <CircleAlert aria-hidden className="size-4 shrink-0 text-amber-600" />;
  return <Bot aria-hidden className="size-4 shrink-0 text-ink-muted" />;
}

function phaseOf(run: RunViewState, snapshot: CollaborationSnapshot) {
  const status = String(snapshot.status ?? "pending");
  return (!isRunActive(run.status) || run.status === "cancelling") && ["pending", "running", "verifying", "delivering"].includes(status) ? "unconfirmed" : status;
}

function deliveryPhase(run: RunViewState, snapshot: CollaborationSnapshot) {
  const phase = phaseOf(run, snapshot);
  return phase === "running" || phase === "pending" ? "awaiting_members" : phase;
}

function phaseLabel(phase: string) {
  return phase === "awaiting_members" ? "等待成员成果" : labels[phase] ?? phase;
}

function CollaborationCard({run, relatedRuns, snapshot, onContinue, sync}: Props & {snapshot: CollaborationSnapshot}) {
  const [collapsed, setCollapsed] = useState(false);
  const [canvas, setCanvas] = useState(false);
  const [selected, setSelected] = useState<string>();
  const [detail, setDetail] = useState<Record<string, unknown>>();
  const [detailError, setDetailError] = useState(false);
  const [detailAttempt, setDetailAttempt] = useState(0);
  const conversationId = run.events.find(event => event.conversation_id)?.conversation_id;
  useEffect(() => {
    setDetail(undefined); setDetailError(false);
    if (!selected || !conversationId || !snapshot.details_available) return;
    const controller = new AbortController();
    void api.collaborationDetails(conversationId, String(snapshot.id), selected, controller.signal)
      .then(value => {if (!controller.signal.aborted) setDetail(value);})
      .catch(() => {if (!controller.signal.aborted) setDetailError(true);});
    return () => controller.abort();
  }, [selected, conversationId, snapshot.id, snapshot.revision, snapshot.details_available, detailAttempt]);
  const contentId = useId();
  const tasks = collaborationTasks(snapshot), records = asRecord(snapshot.records), issue = asRecord(snapshot.issue);
  const done = tasks.filter(task => ["accepted", "completed"].includes(String(asRecord(records[String(task.id)]).status))).length;
  const phase = phaseOf(run, snapshot);
  const mode = snapshot.mode === "single" ? "单任务委派" : snapshot.mode === "batch" ? "批量委派" : "计划执行";
  const runs = useMemo(() => Object.values({...relatedRuns, [run.runId]: run})
    .filter(item => item.runId === run.runId || nativeSnapshots(item).some(value => value.id === snapshot.id))
    .sort((a, b) => (a.events[0]?.occurred_at ?? "").localeCompare(b.events[0]?.occurred_at ?? "")), [relatedRuns, run, snapshot.id]);
  const select = (id: string) => {setCanvas(false); setSelected(id);};
  return <section aria-label="成员协作进度" className="my-4 min-w-0 overflow-hidden rounded-2xl border border-line bg-surface shadow-sm">
    <header className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
      <button type="button" className={cx("flex min-w-0 flex-1 items-center gap-2 rounded text-left text-sm text-ink", focus)}
        aria-expanded={!collapsed} aria-controls={contentId} onClick={() => setCollapsed(value => !value)}>
        <StatusIcon phase={phase} />
        <span className="font-medium">{mode}</span>
        <span className="text-xs text-ink-muted">{run.status === "cancelling" ? "正在停止，保留进度" : labels[phase] ?? phase} · {done}/{tasks.length}</span>
        <ChevronDown aria-hidden className={cx("ml-auto size-4 shrink-0 transition-transform", collapsed && "-rotate-90")} />
      </button>
      <button type="button" onClick={() => setCanvas(true)} className={cx("ml-2 inline-flex items-center gap-1.5 rounded-lg bg-accent/10 px-2.5 py-1.5 text-xs text-accent hover:bg-accent/15", focus)}>
        <Expand className="size-3.5" />展开画布
      </button>
    </header>
    {!collapsed && <div id={contentId}>
      <CollaborationGraph run={run} snapshot={snapshot} onSelect={select} selected={selected} />
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line px-4 py-2 text-[11px] text-ink-muted">
        <span>点击成员查看执行链路 · 与任务面板共用任务编号</span><span>按依赖分列，实际并行由调度决定</span>
      </div>
    </div>}
    {asString(issue.reason) && <div className="m-3 rounded-xl bg-amber-500/10 p-3 text-xs text-ink"><strong>{issue.owner === "user" ? "等待继续" : isRunActive(run.status) ? "主 Agent 正在处理" : "需要主 Agent 继续处理"}</strong><p className="mt-1 whitespace-pre-wrap break-words">{asString(issue.reason)}</p></div>}
    {(phase === "unconfirmed" || sync?.error) && <div role="status" className="m-3 rounded-xl bg-surface-strong p-3 text-xs text-ink-muted">
      {sync?.error ? "状态同步失败，当前显示最后已知进度。" : sync?.syncing ? "正在核对后台执行状态…" : "尚未确认最新执行状态，不代表任务已暂停。"}
      {sync && <button type="button" onClick={sync.retry} className={cx("ml-2 rounded text-accent", focus)}>重新同步</button>}
    </div>}
    {!isRunActive(run.status) && ["paused", "blocked"].includes(String(snapshot.status)) && onContinue && <button type="button" className={cx("mb-3 ml-4 rounded-full border border-line px-3 py-1.5 text-xs text-ink hover:bg-surface-strong", focus)}
      onClick={() => onContinue(`请使用 AgentRunStatus 检查执行 ${snapshot.id}，必要时用 TaskUpdate 修正未完成任务，再使用 AgentResumeRun 从保留进度继续，不要重复创建任务。`)}>继续这次协作</button>}
    {snapshot.status === "completed" && <p className="px-4 pb-3 pt-2 text-xs text-ink-muted">{snapshot.mode === "single" || snapshot.mode === "batch" ? "成员结果已返回，任务状态已更新；具体验证情况见报告。" : "计划执行已完成；如有代码成果，已回写本地并保留为未提交修改。"}</p>}
    {(canvas || selected) && <CollaborationDialog wide={canvas} title={canvas ? "协作任务图" : selected === "$result" ? "验证与交付" : "成员执行链路"}
      onClose={() => {setCanvas(false); setSelected(undefined);}}>
      {canvas ? <><p className="px-5 py-3 text-sm text-ink-muted">{asString(snapshot.objective)}</p><CollaborationGraph run={run} snapshot={snapshot} onSelect={select} large /></>
        : <>{snapshot.details_available && !detail && <div className="px-5 pt-4 text-xs text-ink-muted">{detailError ? <>详情加载失败。<button type="button" onClick={() => setDetailAttempt(value => value + 1)} className="ml-2 text-accent">重试</button></> : "正在读取完整报告与验证记录…"}</div>}
          <MemberExecutionPanel run={run} runs={runs} snapshot={snapshot} detail={detail} taskId={selected!} onSelect={select} /></>}
    </CollaborationDialog>}
  </section>;
}

export function CollaborationGraph({run, snapshot, onSelect, selected, large = false}: {
  run: RunViewState; snapshot: CollaborationSnapshot; onSelect: (id: string) => void; selected?: string; large?: boolean;
}) {
  const layout = useMemo(() => collaborationLayout(snapshot), [snapshot]);
  const [zoom, setZoom] = useState(.85);
  const marker = useId().replace(/:/g, "");
  const phase = phaseOf(run, snapshot);
  return <div className="relative bg-canvas/40">
    <div className="flex items-center justify-between gap-2 px-4 py-2">
      <span className="flex items-center gap-1.5 text-[11px] text-ink-muted"><GitBranch className="size-3.5" />任务依赖图</span>
      <div className="flex items-center gap-1 rounded-lg border border-line bg-surface p-0.5">
        <button type="button" aria-label="缩小任务图" disabled={zoom <= .5} onClick={() => setZoom(value => Math.max(.5, value - .15))} className={cx("rounded p-1 text-ink-muted hover:bg-surface-strong disabled:opacity-30", focus)}><Minus className="size-3.5" /></button>
        <span className="w-9 text-center font-mono text-[10px] text-ink-muted">{Math.round(zoom * 100)}%</span>
        <button type="button" aria-label="放大任务图" disabled={zoom >= 1.45} onClick={() => setZoom(value => Math.min(1.45, value + .15))} className={cx("rounded p-1 text-ink-muted hover:bg-surface-strong disabled:opacity-30", focus)}><Plus className="size-3.5" /></button>
        <button type="button" aria-label="重置任务图缩放" onClick={() => setZoom(.85)} className={cx("rounded p-1 text-ink-muted hover:bg-surface-strong", focus)}><RotateCcw className="size-3" /></button>
      </div>
    </div>
    <div tabIndex={0} role="region" aria-label="可滚动的成员任务图" className={cx("scrollbar-thin overflow-auto", focus)}
      style={{maxHeight: large ? "calc(100dvh - 180px)" : 470, backgroundImage: "radial-gradient(rgb(var(--line-strong)) 1px, transparent 1px)", backgroundSize: "18px 18px"}}>
      <div style={{width: layout.width * zoom, height: layout.height * zoom}}>
        <div className="relative origin-top-left" style={{width: layout.width, height: layout.height, transform: `scale(${zoom})`}}>
          {Array.from({length: layout.maxDepth}, (_, index) => <span key={index} className="absolute top-4 text-[11px] text-ink-faint" style={{left: 24 + (index + 1) * 244}}>依赖层级 {index + 1}</span>)}
          <svg aria-hidden className="pointer-events-none absolute inset-0" width={layout.width} height={layout.height}>
            <defs><marker id={marker} markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6" fill="rgb(var(--line-strong))" /></marker></defs>
            {layout.edges.map(edge => <path key={`${edge.from}-${edge.to}`} d={edge.path} fill="none" stroke="rgb(var(--line-strong))" strokeWidth="1.5" markerEnd={`url(#${marker})`} />)}
          </svg>
          {layout.nodes.map(node => {
            const style = {left: node.x, top: node.y, width: GRAPH_CARD_WIDTH, height: GRAPH_CARD_HEIGHT};
            if (!node.task && node.id === "$root") return <div key={node.id} className="absolute rounded-xl border border-line bg-surface/95 p-3 shadow-sm" style={style}>
              <p className="flex items-center gap-2 text-xs font-medium text-ink"><Users className="size-4 text-ink-muted" />你的任务</p>
              <p className="mt-3 line-clamp-3 break-words text-[11px] leading-5 text-ink-muted" title={asString(snapshot.objective)}>{asString(snapshot.objective) || "主 Agent 安排成员协作"}</p>
            </div>;
            const task = node.task, record = task ? asRecord(asRecord(snapshot.records)[node.id]) : {};
            const state = task ? memberPhase(run, snapshot, node.id) : deliveryPhase(run, snapshot);
            const success = ["accepted", "completed"].includes(state), working = ["running", "verifying", "delivering"].includes(state);
            const title = task ? asString(task.subject) || asString(task.objective) || node.id : "验证与交付";
            const summary = task ? asString(record.reason) || asString(record.summary) || asString(task.objective)
              : phase === "completed" ? "结果已返回主 Agent，查看检查记录" : "收集成员成果、执行检查并返回结果";
            return <button type="button" key={node.id} data-task-id={task ? node.id : undefined} onClick={() => onSelect(node.id)}
              aria-label={task ? `查看任务 #${node.id} ${title} 的执行链路` : "查看验证与交付记录"}
              title={title} className={cx("absolute rounded-xl border bg-surface p-3 text-left shadow-sm transition hover:-translate-y-0.5 hover:shadow-md", focus,
                success ? "border-emerald-500/60" : working ? "border-accent/60" : ["failed", "blocked"].includes(state) ? "border-amber-500/60" : "border-line",
                selected === node.id && "ring-2 ring-accent/30")} style={style}>
              <div className="flex items-start gap-2"><StatusIcon phase={state} /><span className="line-clamp-2 text-xs font-semibold leading-4 text-ink">{title}</span></div>
              <div className="mt-1.5 flex items-center justify-between gap-1 text-[10px] text-ink-muted"><span>{task ? `#${node.id} · ${memberRoles[String(task.profile)] ?? "成员"}` : "执行器"}</span><span className={working ? "text-accent" : success ? "text-emerald-600" : ""}>{phaseLabel(state)}</span></div>
              <p className="mt-2 line-clamp-2 break-words text-[10px] leading-4 text-ink-muted">{summary}</p>
            </button>;
          })}
        </div>
      </div>
    </div>
    {layout.unresolved && <p className="px-4 py-2 text-xs text-warning">部分历史依赖无法排序，已保留任务卡片，请检查任务关系。</p>}
  </div>;
}

function CollaborationDialog({title, children, onClose, wide}: {title: string; children: ReactNode; onClose: () => void; wide?: boolean}) {
  const ref = useRef<HTMLDialogElement>(null), titleId = useId();
  useEffect(() => {const dialog = ref.current; dialog?.showModal(); return () => {dialog?.close();};}, []);
  return createPortal(<dialog ref={ref} aria-labelledby={titleId} onCancel={event => {event.preventDefault(); onClose();}}
    onClick={event => {if (event.target === event.currentTarget) {const rect = event.currentTarget.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose();}}}
    className={cx("fixed border border-line bg-surface p-0 text-ink shadow-2xl backdrop:bg-black/25", wide ? "inset-0 m-auto h-[min(850px,94dvh)] max-h-[94dvh] w-[96vw] max-w-[1500px] rounded-2xl" : "inset-y-0 left-auto right-0 m-0 h-dvh max-h-none w-[min(580px,100vw)] max-w-none rounded-l-2xl")}>
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 items-center justify-between border-b border-line px-5 py-4"><h2 id={titleId} className="text-sm font-semibold">{title}</h2>
        <button type="button" autoFocus aria-label="关闭执行面板" className={cx("rounded-lg p-1.5 text-ink-muted hover:bg-surface-strong", focus)} onClick={onClose}><X className="size-4" /></button></header>
      <div className="scrollbar-thin min-h-0 flex-1 overflow-auto">{children}</div>
    </div>
  </dialog>, document.body);
}

export function MemberExecutionPanel({run, runs = [run], snapshot, detail, taskId, onSelect}: {
  run: RunViewState; runs?: RunViewState[]; snapshot: CollaborationSnapshot; detail?: Record<string, unknown>; taskId: string; onSelect?: (id: string) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const task = detail?.task ? asRecord(detail.task) : collaborationTasks(snapshot).find(task => task.id === taskId);
  const record = detail?.record ? asRecord(detail.record) : asRecord(asRecord(snapshot.records)[taskId]);
  const phase = task ? memberPhase(run, snapshot, taskId) : deliveryPhase(run, snapshot);
  const traces = useMemo(() => task ? runs.map(item => {
    const view = memberTrace(item, snapshot, taskId);
    return {runId: item.runId, ...buildProcessPresentation(view), active: isRunActive(view.status)};
  }).filter(trace => trace.entries.length) : [], [runs, taskId, snapshot, task]);
  const entries = traces.flatMap(trace => trace.entries.map(entry => ({entry, active: trace.active, key: `${trace.runId}:${entry.id}`})));
  const visible = showAll ? entries : entries.slice(-30);
  const emitted = runs.flatMap(item => item.events).filter(event => event.type === "collaboration.check" && event.payload.id === snapshot.id).map(event => event.payload);
  const checks = (Array.isArray(detail?.checks) ? detail.checks.map(asRecord) : emitted.length ? emitted : Array.isArray(snapshot.checks) ? snapshot.checks.map(asRecord) : [])
    .filter(check => task ? check.task_id === taskId : true);
  const title = task ? `任务 #${taskId} · ${asString(task.subject) || asString(task.objective)}` : "验证与交付";
  return <div className="space-y-5 p-5">
    <div><h3 className="break-words text-base font-semibold text-ink">{title}</h3>
      <div className="mt-2 flex items-center gap-2 text-xs text-ink-muted"><StatusIcon phase={phase} /><span>{phaseLabel(phase)}</span>{task && <span>· {memberRoles[String(task.profile)] ?? "成员"} · {Number(record.attempts ?? 0) > 0 ? `第 ${Number(record.attempts)} 次执行` : "尚未执行"}</span>}</div>
      {task && <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-6 text-ink-muted">{asString(task.objective)}</p>}
      {task && dependencies(task).length > 0 && <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-ink-muted">前置任务：{dependencies(task).map(id => <button type="button" key={id} onClick={() => onSelect?.(id)} className={cx("rounded border border-line px-2 py-1 hover:text-accent", focus)}>#{id}</button>)}</div>}
    </div>
    {asString(record.reason) && <p className="rounded-xl bg-surface-strong p-3 text-xs leading-5 text-ink-muted">{asString(record.reason)}</p>}
    {asString(record.summary) && <section><h4 className="mb-2 text-xs font-semibold text-ink">成员报告</h4><div className="markdown break-words rounded-xl border border-line p-3 text-sm"><ReactMarkdown remarkPlugins={[remarkGfm]}>{asString(record.summary)}</ReactMarkdown></div></section>}
    {task && <section><h4 className="mb-3 text-xs font-semibold text-ink">执行链路 · {entries.length} 项活动</h4>
      {entries.length > 30 && <button type="button" className={cx("mb-3 rounded text-xs text-accent", focus)} onClick={() => setShowAll(value => !value)}>{showAll ? "仅显示最近 30 项" : `展开全部 ${entries.length} 项活动`}</button>}
      <ol className="process-trace space-y-4">{visible.map(({entry, active, key}) => <ProcessStep key={key} entry={{...entry, agentLabel: undefined}} active={active} />)}</ol>
      {!entries.length && <p className="rounded-xl border border-dashed border-line px-4 py-6 text-center text-xs text-ink-muted">{Number(record.attempts ?? 0) ? "暂未收到可展示的执行活动；收到后会自动更新。" : "尚未开始执行，等待前置任务或调度。"}</p>}
    </section>}
    <section><h4 className="mb-3 text-xs font-semibold text-ink">{task ? "任务验证" : "全部验证记录"} · {checks.length} 项</h4>
      {!checks.length && <p className="text-xs leading-5 text-ink-muted">暂无已记录的检查结果，不代表验证已通过。</p>}
      <div className="space-y-2">{checks.map((check, index) => <details key={index} className="rounded-xl border border-line p-3" open={check.status !== "passed"}>
        <summary className="cursor-pointer break-words text-xs text-ink"><span className={check.status === "passed" ? "text-emerald-600" : "text-amber-600"}>{check.status === "passed" ? "通过" : "未通过"}</span> · {asString(check.command)} {typeof check.duration_ms === "number" && `· ${formatDuration(check.duration_ms)}`}</summary>
        {asString(check.task_id) && <p className="mt-2 text-[11px] text-ink-muted">{check.task_id === "delivery" ? "最终交付检查" : `任务 #${check.task_id}`}</p>}
        <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all text-[11px] leading-5 text-ink-muted">{asString(check.output) || "未保存输出"}</pre>
      </details>)}</div>
    </section>
  </div>;
}
