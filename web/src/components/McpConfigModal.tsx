import { useEffect, useRef, useState, type ReactNode } from "react";
import { BookOpen, Check, ChevronDown, CircleAlert, Code2, Globe2, Layers3, Plug, Plus, RefreshCw, Search, Settings2, ShieldCheck, ShieldQuestion, Terminal, Trash2, X } from "lucide-react";
import type { McpConfig, McpServer, McpTransport, SaveMcpServer } from "@/types/api";
import { IconButton, Spinner } from "@/components/ui";
import { cx } from "@/lib/utils";

type Props = {
  embedded?: boolean;
  open: boolean;
  workspace?: string;
  config?: McpConfig;
  loading?: boolean;
  saving?: boolean;
  deleting?: string;
  actionName?: string;
  refreshing?: boolean;
  error?: string;
  message?: string;
  onSave: (server: SaveMcpServer) => void | Promise<void>;
  onDelete: (name: string) => void;
  onAction?: (name: string, fingerprint: string, action: "trust" | "connect", approved?: boolean) => void;
  onRefresh?: () => void;
  onClose: () => void;
};

type FormState = {
  name: string; transport: McpTransport; command: string; args: string;
  cwd: string; url: string; env: string; headers: string;
  startup_timeout: number; call_timeout: number;
  lifecycle: "shared" | "execution";
};
const emptyForm: FormState = { name: "", transport: "http", command: "", args: "", cwd: "", url: "", env: "", headers: "", startup_timeout: 15, call_timeout: 60, lifecycle: "shared" };
const presets = [
  { name: "Context7", description: "获取最新开发文档", icon: BookOpen, patch: { name: "context7", url: "https://mcp.context7.com/mcp" } },
  { name: "GitHub", description: "连接代码仓库与 Issue", icon: Code2, patch: { name: "github", url: "https://api.githubcopilot.com/mcp/", headers: "Authorization=Bearer ${GITHUB_PAT}" } },
  { name: "浏览器（可见）", description: "打开独立窗口，查看操作过程", icon: Globe2, patch: { name: "playwright", transport: "stdio" as const, command: "npx", args: "-y\n@playwright/mcp@0.0.83\n--browser\nchrome\n--isolated", lifecycle: "execution" as const, startup_timeout: 120 } },
  { name: "浏览器（后台）", description: "后台测试，不显示窗口", icon: Globe2, patch: { name: "playwright-headless", transport: "stdio" as const, command: "npx", args: "-y\n@playwright/mcp@0.0.83\n--browser\nchrome\n--isolated\n--headless", lifecycle: "execution" as const, startup_timeout: 120 } },
];
const statuses: Record<string, { label: string; color: string }> = {
  connected: { label: "已连接", color: "text-success bg-success/10" },
  connecting: { label: "连接中", color: "text-accent bg-accent/10" },
  awaiting_trust: { label: "待信任", color: "text-amber-700 bg-amber-500/10 dark:text-amber-300" },
  not_connected: { label: "未连接", color: "text-ink-muted bg-surface-strong" },
  disconnected: { label: "已断开", color: "text-ink-muted bg-surface-strong" },
  error: { label: "连接异常", color: "text-danger bg-danger/10" },
};

export function McpConfigModal(props: Props) {
  const [form, setForm] = useState<FormState>(emptyForm);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<McpServer>();
  const [clearCredentials, setClearCredentials] = useState(false);
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState<string>();
  const [formError, setFormError] = useState<string>();
  const firstInput = useRef<HTMLInputElement>(null);
  const panel = useRef<HTMLElement>(null);
  const addButton = useRef<HTMLButtonElement>(null);
  const busy = Boolean(props.saving || props.deleting || props.actionName);
  const servers = props.config?.servers ?? [];
  const connected = servers.filter(server => server.status === "connected").length;
  const pending = servers.filter(server => !server.trusted).length;
  const filtered = servers.filter(server => `${server.name} ${server.url} ${server.command}`.toLowerCase().includes(search.trim().toLowerCase()));

  useEffect(() => { if (adding) firstInput.current?.focus(); }, [adding]);
  useEffect(() => {
    if (!props.open) { setAdding(false); setEditing(undefined); setForm(emptyForm); setFormError(undefined); }
    if (!props.open || props.embedded) return;
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) props.onClose();
      if (event.key !== "Tab") return;
      const items = [...(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input, select, textarea, a[href], summary, [tabindex="0"]') ?? [])];
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); previous?.focus(); };
  }, [props.open, props.embedded, props.onClose, busy]);

  if (!props.open) return null;
  const browserArg = lines(form.args).indexOf("--browser");
  const update = (patch: Partial<FormState>) => setForm(current => ({ ...current, ...patch }));
  const closeForm = () => { setAdding(false); setEditing(undefined); setFormError(undefined); requestAnimationFrame(() => addButton.current?.focus()); };
  const openForm = (patch: Partial<FormState> = {}) => { setEditing(undefined); setClearCredentials(false); setForm({ ...emptyForm, ...patch }); setAdding(true); setFormError(undefined); };
  const editServer = (server: McpServer) => {
    setEditing(server); setClearCredentials(false); setAdding(true); setFormError(undefined);
    setForm({ ...emptyForm, name: server.name, transport: server.transport, command: server.command,
      args: server.args.join("\n"), cwd: server.cwd || "", url: server.url,
      lifecycle: server.lifecycle ?? "shared",
      startup_timeout: server.startup_timeout, call_timeout: server.call_timeout });
    requestAnimationFrame(() => firstInput.current?.focus());
  };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!props.workspace || busy) return;
    try {
      setFormError(undefined);
      if (!editing && servers.some(server => server.name === form.name.trim())) throw new Error("此名称已存在，请为新服务使用不同的名称。");
      await props.onSave({ workspace: props.workspace, name: form.name.trim(), transport: form.transport,
        command: form.command.trim(), args: lines(form.args), cwd: form.cwd.trim() || undefined,
        url: form.url.trim(), fingerprint: editing?.fingerprint,
        lifecycle: form.transport === "stdio" ? form.lifecycle : "shared",
        env: clearCredentials ? {} : form.env.trim() ? keyValues(form.env) : editing ? undefined : {},
        headers: clearCredentials ? {} : form.headers.trim() ? keyValues(form.headers) : editing ? undefined : {},
        startup_timeout: form.startup_timeout, call_timeout: form.call_timeout });
      closeForm(); setSearch(""); setExpanded(form.name.trim());
    } catch (error) { setFormError(error instanceof Error ? error.message : "保存失败，请重试。"); }
  };

  return <div className={props.embedded ? "" : "fixed inset-0 z-[80] grid place-items-center bg-black/55 p-4 backdrop-blur-sm"}
    onMouseDown={event => !props.embedded && !busy && event.target === event.currentTarget && props.onClose()}>
    <section ref={panel} tabIndex={-1} role={props.embedded ? undefined : "dialog"} aria-modal={props.embedded ? undefined : true} aria-label="MCP 服务管理"
      className={cx("min-w-0 outline-none", !props.embedded && "max-h-[92vh] w-full max-w-3xl overflow-y-auto rounded-2xl border border-line bg-canvas p-6 shadow-2xl")}>
      {!props.embedded && <div className="mb-6 flex items-center justify-between"><h2 className="text-xl font-semibold">MCP 服务</h2><IconButton label="关闭 MCP 配置" onClick={props.onClose}><X className="size-4" /></IconButton></div>}
      <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3 text-xs text-ink-muted"><span className="inline-flex items-center gap-1.5"><span className="size-1.5 rounded-full bg-success" />{connected} 已连接</span><span className="text-line">/</span><span>{servers.length} 个服务</span>{pending > 0 && <span className="rounded-md bg-amber-500/10 px-2 py-1 text-amber-700 dark:text-amber-300">{pending} 待信任</span>}</div>
        <div className="flex gap-2"><IconButton label="刷新服务状态" disabled={props.refreshing} onClick={props.onRefresh}><RefreshCw className={cx("size-4", props.refreshing && "animate-spin")} /></IconButton>
          {!adding && <button ref={addButton} type="button" className={primaryButton} disabled={!props.workspace || busy} onClick={() => openForm()}><Plus className="size-4" />添加服务</button>}</div>
      </div>
      {props.error && <Notice error>{props.error}</Notice>}
      {props.message && <Notice>{props.message}</Notice>}

      {adding && <form onSubmit={submit} className="mb-6 overflow-hidden rounded-2xl border border-accent/30 bg-surface shadow-sm">
        <div className="flex items-center justify-between border-b border-line px-5 py-4"><div><h3 className="text-sm font-semibold">{editing ? `编辑 ${editing.name}` : "添加 MCP 服务"}</h3><p className="mt-1 text-xs text-ink-muted">{editing ? "修改配置后，需要重新确认信任。" : "连接你需要的工具，从一个服务开始。"}</p></div><IconButton label="取消添加" disabled={busy} onClick={closeForm}><X className="size-4" /></IconButton></div>
        <fieldset disabled={busy} className="space-y-5 p-5">
          {!editing && <div className="grid gap-2 sm:grid-cols-2">{presets.map(preset => <button key={preset.name} type="button" onClick={() => { setForm({ ...emptyForm, ...preset.patch }); firstInput.current?.focus(); }} className="group flex items-center gap-3 rounded-xl border border-line bg-canvas px-3 py-3 text-left transition hover:border-accent/40 hover:bg-accent/5"><preset.icon className="size-5 shrink-0 text-ink-muted group-hover:text-accent" /><span><span className="block text-xs font-medium">{preset.name}</span><span className="mt-1 block text-xs text-ink-faint">{preset.description}</span></span></button>)}</div>}
          <Field label="服务名称"><input ref={firstInput} readOnly={Boolean(editing)} required maxLength={64} pattern="[A-Za-z0-9_-]+" value={form.name} onChange={event => update({ name: event.target.value })} className={inputClass} placeholder="例如 my-docs（字母、数字、- 或 _）" /></Field>
          <div className="grid grid-cols-2 gap-3" role="group" aria-label="连接方式">{([{ type: "http", title: "远程服务", hint: "通过 URL 连接", icon: Globe2 }, { type: "stdio", title: "本地服务", hint: "运行本机命令", icon: Terminal }] as const).map(item => <button key={item.type} type="button" aria-pressed={form.transport === item.type} onClick={() => update({ transport: item.type })} className={cx("flex items-center gap-3 rounded-xl border p-3 text-left transition", form.transport === item.type ? "border-accent bg-accent/5 text-accent" : "border-line text-ink-muted hover:bg-surface-muted")}><item.icon className="size-5 shrink-0" /><span className="min-w-0 flex-1"><span className="block text-sm font-medium">{item.title}</span><span className="mt-0.5 block text-xs text-ink-muted">{item.hint}</span></span>{form.transport === item.type && <Check className="size-4 shrink-0" />}</button>)}</div>
          {form.transport === "http" ? <Field label="服务地址"><input required type="url" value={form.url} onChange={event => update({ url: event.target.value })} className={inputClass} placeholder="https://example.com/mcp" /></Field> : <div className="space-y-4"><Field label="启动命令"><input required value={form.command} onChange={event => update({ command: event.target.value })} className={inputClass} placeholder="npx、python、docker…" /></Field><Field label="启动参数" hint="每行一个参数"><textarea value={form.args} onChange={event => update({ args: event.target.value })} className={textareaClass} placeholder={"-y\n@company/mcp-server"} /></Field></div>}
          {form.transport === "stdio" && <div className="space-y-3 rounded-xl border border-line p-4">
            <label className="flex items-start gap-2 text-xs leading-5 text-ink-muted"><input type="checkbox" className="mt-1" checked={form.lifecycle === "execution"} onChange={event => update({ lifecycle: event.target.checked ? "execution" : "shared" })} /><span>每次任务独占使用，结束后关闭服务<span className="block text-ink-faint">普通服务按任务隔离；受支持的浏览器预设在 Web 中按会话隔离并保留页面。</span></span></label>
            {form.args.includes("@playwright/mcp") && <>
              <Field label="使用的浏览器"><select className={inputClass} value={browserArg < 0 ? "chrome" : (lines(form.args)[browserArg + 1] || "chrome")} onChange={event => { const args = lines(form.args); const index = args.indexOf("--browser"); if (index >= 0) args.splice(index + 1, 1, event.target.value); else args.push("--browser", event.target.value); update({ args: args.join("\n") }); }}><option value="chrome">Google Chrome</option><option value="msedge">Microsoft Edge</option></select></Field>
              <p className="text-xs leading-5 text-ink-faint">需要本机安装 Node.js 18+ 和所选浏览器。首次连接会下载工具，可能需要稍等。预设使用独立临时环境。Web 会话内跨轮保留页面和登录状态，可在右侧浏览器面板接管；闲置 30 分钟、取消或关闭后清理。自定义启动参数不保证支持此功能。</p>
            </>}
          </div>}
          <details className="group rounded-xl border border-line" open={form.headers ? true : undefined}><summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3 text-xs font-medium text-ink-muted">高级设置 · 凭据与超时<ChevronDown className="size-4 transition group-open:rotate-180" /></summary><div className="space-y-4 border-t border-line p-4">
            {editing && <div className="space-y-2 text-xs leading-5 text-ink-muted"><p>凭据留空会保留已保存的值；填写新内容会替换对应凭据。</p><label className="flex items-center gap-2"><input type="checkbox" checked={clearCredentials} onChange={event => setClearCredentials(event.target.checked)} />清除已保存的凭据</label></div>}
            {form.transport === "http" ? <Field label="请求头" hint="每行 KEY=value，支持环境变量引用"><textarea disabled={clearCredentials} value={form.headers} onChange={event => update({ headers: event.target.value })} className={textareaClass} placeholder="Authorization=Bearer ${API_TOKEN}" /></Field> : <><Field label="工作目录" hint="留空使用当前项目"><input value={form.cwd} onChange={event => update({ cwd: event.target.value })} className={inputClass} placeholder={props.workspace} /></Field><Field label="环境变量" hint="每行 KEY=value，建议引用已有环境变量"><textarea disabled={clearCredentials} value={form.env} onChange={event => update({ env: event.target.value })} className={textareaClass} placeholder="API_TOKEN=${API_TOKEN}" /></Field></>}
            <div className="grid gap-4 sm:grid-cols-2"><Field label="启动时限（秒）"><input type="number" min={1} max={300} required value={form.startup_timeout} onChange={event => update({ startup_timeout: Number(event.target.value) })} className={inputClass} /></Field><Field label="调用时限（秒）"><input type="number" min={1} max={3600} required value={form.call_timeout} onChange={event => update({ call_timeout: Number(event.target.value) })} className={inputClass} /></Field></div>
          </div></details>
          {formError && <Notice error>{formError}</Notice>}
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4"><p className="flex items-center gap-1.5 text-xs text-ink-muted"><ShieldCheck className="size-4" />{editing ? "修改后需重新信任此配置。" : "保存后，确认信任才会启动服务。"}</p><div className="flex gap-2"><button type="button" className={secondaryButton} onClick={closeForm}>取消</button><button type="submit" disabled={!props.workspace || busy} className={primaryButton}>{props.saving ? <Spinner className="size-4" /> : <Plus className="size-4" />}保存服务</button></div></div>
        </fieldset>
      </form>}

      {servers.length > 3 && <label className="mb-4 flex items-center gap-2 rounded-xl border border-line bg-surface px-3"><Search className="size-4 text-ink-faint" /><input aria-label="搜索 MCP 服务" value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索服务名称或地址" className="h-10 min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-ink-faint" /></label>}
      {props.loading ? <div className="flex items-center justify-center gap-2 rounded-2xl border border-line bg-surface py-16 text-sm text-ink-muted" role="status"><Spinner />正在读取服务…</div> : !servers.length ? <div className="rounded-2xl border border-dashed border-line bg-surface px-6 py-12 text-center"><div className="relative mx-auto mb-5 grid size-16 place-items-center rounded-2xl border border-line bg-canvas"><Plug className="size-7 text-ink-muted" /><span className="absolute -bottom-1 -right-1 grid size-6 place-items-center rounded-full border-4 border-surface bg-accent text-white"><Plus className="size-3" /></span></div><h3 className="text-base font-semibold">为你的 Agent 连接更多工具</h3><p className="mx-auto mt-2 max-w-sm text-sm leading-6 text-ink-muted">从开发文档、代码仓库到浏览器自动化，<br className="hidden sm:block" />把日常使用的服务接入当前项目。</p>{!adding && <button type="button" className={cx(primaryButton, "mt-6")} disabled={!props.workspace} onClick={() => openForm()}><Plus className="size-4" />添加第一个服务</button>}</div> : <div className="space-y-3">{filtered.map(server => {
        const state = statuses[server.status] ?? statuses.not_connected!;
        const showDetails = expanded === server.name;
        const actionBusy = props.actionName === server.name;
        return <article key={server.name} className="overflow-hidden rounded-xl border border-line bg-surface transition hover:border-ink-faint/30">
          <div className="flex items-start gap-3 p-4 sm:gap-4 sm:p-5"><div className="grid size-10 shrink-0 place-items-center rounded-xl border border-line bg-canvas text-ink-muted">{server.transport === "http" ? <Globe2 className="size-5" /> : <Terminal className="size-5" />}</div><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><h3 className="break-all text-sm font-semibold">{server.name}</h3><span className={cx("inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium", state.color)}><span className="size-1.5 rounded-full bg-current" />{actionBusy ? "处理中" : state.label}</span></div><p className="mt-1.5 truncate font-mono text-xs text-ink-muted" title={server.url || server.command}>{server.url || [server.command, ...server.args].join(" ")}</p><div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-faint"><span>{server.transport === "http" ? "远程 HTTP" : "本地命令"}</span>{server.lifecycle === "execution" && <span>{server.browser_session_supported ? "会话独占 · 跨轮保留页面" : "任务独占 · 结束后关闭"}</span>}{server.status === "connected" && <span className="inline-flex items-center gap-1"><Layers3 className="size-3" />{server.tool_count} 个工具</span>}<button type="button" aria-expanded={showDetails} onClick={() => setExpanded(showDetails ? undefined : server.name)} className="inline-flex items-center gap-1 rounded text-ink-muted hover:text-accent">{showDetails ? "收起详情" : "查看配置"}<ChevronDown className={cx("size-3", showDetails && "rotate-180")} /></button></div></div>
            <div className="flex shrink-0 items-center gap-1">{actionBusy ? <Spinner className="mx-2 size-4" /> : !server.trusted ? <button type="button" disabled={busy || !props.onAction || Boolean(server.error && !server.command && !server.url)} onClick={() => setExpanded(server.name)} className={secondaryButton}>检查配置</button> : server.status !== "connected" && <button type="button" disabled={busy || !props.onAction} onClick={() => props.onAction?.(server.name, server.fingerprint, "connect")} className={secondaryButton}>{server.status === "error" ? "重试" : "连接"}</button>}<IconButton label={`删除 ${server.name}`} disabled={busy} onClick={() => window.confirm(`删除 ${server.name} 的配置并撤销信任？已发送的操作可能仍在执行。`) && props.onDelete(server.name)} className="text-ink-faint hover:text-danger">{props.deleting === server.name ? <Spinner className="size-4" /> : <Trash2 className="size-4" />}</IconButton></div>
          </div>
          {server.error && <p role="alert" className="mx-4 mb-4 flex items-start gap-2 rounded-lg bg-danger/5 p-3 text-xs leading-5 text-danger sm:mx-5"><CircleAlert className="mt-0.5 size-4 shrink-0" />{server.error}</p>}
          {showDetails && <div className="border-t border-line bg-canvas/50 p-4 sm:p-5"><ServerDetails server={server} workspace={props.workspace} /><button type="button" disabled={busy} className={cx(secondaryButton, "mt-4")} onClick={() => editServer(server)}><Settings2 className="size-4" />编辑配置</button><div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4"><p className="max-w-md text-xs leading-5 text-ink-muted">{server.trusted ? "已信任此配置。工具调用仍遵循现有权限审批。" : "请核对命令、地址和变量。信任后，服务可在本机启动或向此地址建立连接；配置变化后需重新确认。"}</p><button type="button" disabled={busy || !props.onAction || Boolean(server.error && !server.command && !server.url)} className={server.trusted ? secondaryButton : primaryButton} onClick={() => props.onAction?.(server.name, server.fingerprint, "trust", !server.trusted)}>{server.trusted ? <ShieldQuestion className="size-4" /> : <ShieldCheck className="size-4" />}{server.trusted ? "撤销信任" : "信任此配置"}</button></div></div>}
        </article>;
      })}{filtered.length === 0 && <div className="py-10 text-center text-sm text-ink-muted">没有匹配的服务。<button className="ml-2 text-accent" onClick={() => setSearch("")}>清除搜索</button></div>}</div>}
      <div className="mt-6 flex items-start gap-2 px-1 text-xs leading-5 text-ink-faint"><ShieldCheck className="mt-0.5 size-4 shrink-0" /><p>信任仅对当前项目和这份配置有效。一个服务连接失败，不影响其他服务。<span className="mt-1 block break-all font-mono" title={props.config?.config_path}>{props.config?.config_path}</span></p></div>
    </section>
  </div>;
}

function ServerDetails({ server, workspace }: { server: McpServer; workspace?: string }) {
  return <dl className="grid min-w-0 gap-4 text-xs sm:grid-cols-2"><div className="min-w-0 sm:col-span-2"><dt className="mb-1.5 text-ink-faint">{server.transport === "http" ? "服务地址" : "启动命令与参数"}</dt><dd className="whitespace-pre-wrap break-all rounded-lg border border-line bg-surface p-3 font-mono leading-6">{server.url || [server.command, ...server.args].join("\n")}</dd></div>{server.transport === "stdio" && <div className="min-w-0 sm:col-span-2"><dt className="text-ink-faint">工作目录</dt><dd className="mt-1 break-all font-mono">{server.cwd || workspace}</dd></div>}<div><dt className="text-ink-faint">启动 / 调用时限</dt><dd className="mt-1">{server.startup_timeout} 秒 / {server.call_timeout} 秒</dd></div><div><dt className="text-ink-faint">凭据变量（值已隐藏）</dt><dd className="mt-1 break-all font-mono">{[...server.env_keys, ...server.header_keys].join(", ") || "未配置"}</dd></div></dl>;
}
function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <label className="block"><span className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs font-medium text-ink">{label}{hint && <span className="font-normal text-ink-faint">{hint}</span>}</span>{children}</label>;
}
function Notice({ error, children }: { error?: boolean; children: ReactNode }) {
  return <div role={error ? "alert" : "status"} className={cx("mb-4 flex items-start gap-2 rounded-xl border px-4 py-3 text-xs leading-5", error ? "border-danger/20 bg-danger/5 text-danger" : "border-accent/15 bg-accent/5 text-ink-muted")}>{error ? <CircleAlert className="mt-0.5 size-4 shrink-0" /> : <Check className="mt-0.5 size-4 shrink-0 text-accent" />}{children}</div>;
}
function lines(value: string) { return value.split(/\r?\n/).map(item => item.trim()).filter(Boolean); }
function keyValues(value: string) {
  const result: Record<string, string> = {};
  for (const line of lines(value)) {
    const separator = line.indexOf("=");
    if (separator <= 0) throw new Error("请使用 KEY=value 格式填写凭据，每行一项。");
    result[line.slice(0, separator).trim()] = line.slice(separator + 1).trim();
  }
  return result;
}
const inputClass = "h-11 w-full rounded-lg border border-line bg-canvas px-3 text-sm text-ink outline-none transition placeholder:text-xs placeholder:text-ink-faint focus:border-accent focus:ring-2 focus:ring-accent/10";
const textareaClass = "scrollbar-thin min-h-24 w-full resize-y rounded-lg border border-line bg-canvas px-3 py-2 font-mono text-xs leading-6 text-ink outline-none transition placeholder:text-ink-faint focus:border-accent focus:ring-2 focus:ring-accent/10";
const primaryButton = "inline-flex min-h-9 items-center justify-center gap-2 rounded-lg bg-accent px-3.5 py-2 text-xs font-medium text-white transition hover:bg-accent-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50";
const secondaryButton = "inline-flex min-h-9 items-center justify-center gap-1.5 rounded-lg border border-line bg-surface px-3 py-2 text-xs font-medium text-ink-muted transition hover:border-accent/40 hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-50";
