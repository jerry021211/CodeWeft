import { useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowLeft, AudioLines, Bot, Check, CircleAlert, Database, KeyRound, RefreshCw, Save, Settings2, PlugZap } from "lucide-react";
import { api } from "@/lib/api";
import { cx } from "@/lib/utils";
import { modelServiceLabels, modelSettingsDraft, modelSettingsError } from "@/lib/modelSettings";
import type { ModelServiceName, ModelSettings, ModelSettingsInput } from "@/types/api";
import { Spinner } from "@/components/ui";

const fieldClass = "h-10 w-full rounded-xl border border-line bg-canvas px-3 text-sm text-ink outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/10 disabled:opacity-50";
const icons = { chat: Bot, speech: AudioLines, embedding: Database };
const descriptions = {
  chat: "负责对话、工具调用和任务执行。主 Agent、子 Agent、摘要与记忆共用这一模型。",
  speech: "将上传的 WAV / MP3 转为文字，再交给对话模型处理。可以使用不同的模型服务。",
  embedding: "为代码检索生成向量，让搜索理解代码语义。需要与服务实际输出一致的向量维度。",
};

export function ModelSettingsPage({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [settings, setSettings] = useState<ModelSettings>();
  const [draft, setDraft] = useState<ModelSettingsInput>();
  const [section, setSection] = useState<ModelServiceName>("chat");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [discovering, setDiscovering] = useState<ModelServiceName>();
  const [testing, setTesting] = useState<ModelServiceName>();
  const [models, setModels] = useState<Partial<Record<ModelServiceName, string[]>>>({});
  const [connection, setConnection] = useState<Partial<Record<ModelServiceName, { ok: boolean; message: string }>>>({});
  const [discoveryStatus, setDiscoveryStatus] = useState<Partial<Record<ModelServiceName, string>>>({});
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const alive = useRef(true);
  const dirty = Boolean(settings && draft && JSON.stringify(modelSettingsDraft(settings)) !== JSON.stringify(draft));
  const busy = saving || Boolean(discovering) || Boolean(testing) || loading;

  const load = async () => {
    setLoading(true); setError(""); setMessage("");
    try {
      const result = await api.getModelSettings();
      if (alive.current) { setSettings(result); setDraft(modelSettingsDraft(result)); setModels({}); setConnection({}); setDiscoveryStatus({}); }
    } catch (error) { if (alive.current) setError(error instanceof Error ? error.message : "读取配置失败"); }
    finally { if (alive.current) setLoading(false); }
  };
  useEffect(() => { alive.current = true; void load(); return () => { alive.current = false; }; }, []);

  const update = <K extends ModelServiceName>(name: K, fields: Partial<ModelSettingsInput["services"][K]>) => {
    setDraft(current => current ? { ...current, services: { ...current.services, [name]: { ...current.services[name], ...fields } } } : current);
    setConnection(current => ({ ...current, [name]: undefined }));
    if ("base_url" in fields || "api_key" in fields || "protocol" in fields) {
      setDiscoveryStatus(current => ({ ...current, [name]: undefined }));
      setModels(current => ({ ...current, [name]: undefined }));
    }
    setError(""); setMessage("");
  };
  const save = async () => {
    if (!draft || busy) return;
    const validation = modelSettingsError(draft);
    if (validation) { setError(validation); return; }
    setSaving(true); setError(""); setMessage("");
    try {
      const result = await api.saveModelSettings({ ...draft, services: { ...draft.services, chat: { ...draft.services.chat, reasoning_levels: draft.services.chat.reasoning_levels.filter(Boolean) } } });
      if (alive.current) { setSettings(result); setDraft(modelSettingsDraft(result)); setMessage("配置已保存并生效，下一次任务将使用新配置。"); onSaved(); }
    } catch (error) { if (alive.current) setError(error instanceof Error ? error.message : "保存失败"); }
    finally { if (alive.current) setSaving(false); }
  };
  const discover = async (name: ModelServiceName) => {
    if (!draft || busy) return;
    setDiscovering(name); setDiscoveryStatus(current => ({ ...current, [name]: undefined }));
    try {
      const result = await api.discoverModels(draft, name);
      if (alive.current) { setModels(current => ({ ...current, [name]: result.models })); setDiscoveryStatus(current => ({ ...current, [name]: `已获取 ${result.models.length} 个模型；请选择或手动填写模型名称，再测试连接。` })); }
    } catch (error) {
      if (alive.current) setDiscoveryStatus(current => ({ ...current, [name]: error instanceof Error ? error.message : "获取模型列表失败" }));
    } finally { if (alive.current) setDiscovering(undefined); }
  };

  const testConnection = async (name: ModelServiceName) => {
    if (!draft || busy) return;
    setTesting(name); setConnection(current => ({ ...current, [name]: undefined }));
    try {
      const result = await api.testModelConnection(draft, name);
      if (alive.current) setConnection(current => ({ ...current, [name]: { ok: result.ok, message: `${result.message}（耗时 ${(result.elapsed_ms / 1000).toFixed(2)} 秒）` } }));
    } catch (error) {
      if (alive.current) setConnection(current => ({ ...current, [name]: { ok: false, message: error instanceof Error ? error.message : "测试失败" } }));
    } finally { if (alive.current) setTesting(undefined); }
  };

  const service = draft?.services[section];
  const storedKey = Boolean(settings?.services[section].has_api_key);
  const Icon = icons[section];
  return <main className="fixed inset-0 z-[60] flex flex-col bg-canvas text-ink" aria-label="模型设置">
    <header className="shrink-0 border-b border-line bg-surface">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-3 px-4 sm:px-8">
        <button type="button" onClick={onClose} disabled={busy} className="inline-flex items-center gap-2 text-sm text-ink-muted hover:text-ink disabled:opacity-40"><ArrowLeft className="size-4" />返回对话</button>
        <span className="inline-flex items-center gap-2 text-sm font-semibold"><Settings2 className="size-4 text-accent" />设置</span>
        <span className="text-xs text-ink-faint">本机配置</span>
      </div>
    </header>
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-7 sm:px-8 sm:py-10">
        <div className="mb-7"><h1 className="text-2xl font-semibold tracking-tight">模型与能力</h1><p className="mt-2 text-sm leading-6 text-ink-muted">选择一个对话模型，再按需接入语音识别和向量检索。</p></div>
        {loading && <div className="flex items-center gap-3 rounded-2xl border border-line bg-surface p-8 text-sm text-ink-muted"><Spinner />正在读取模型配置…</div>}
        {!loading && draft && <div className="grid gap-5 md:grid-cols-[220px_minmax(0,1fr)]">
          <nav aria-label="模型能力" className="flex gap-2 md:flex-col">
            {(["chat", "speech", "embedding"] as const).map(name => {
              const NavIcon = icons[name]; const item = draft.services[name];
              const enabled = name === "chat" || "enabled" in item && item.enabled;
              return <button key={name} type="button" aria-current={section === name ? "page" : undefined} onClick={() => setSection(name)}
                className={cx("flex min-w-0 flex-1 items-center gap-3 rounded-xl border p-3 text-left transition md:flex-none", section === name ? "border-accent/25 bg-accent/10 text-accent" : "border-transparent text-ink-muted hover:bg-surface")}>
                <NavIcon className="size-4 shrink-0" /><span className="min-w-0"><span className="block text-xs font-medium sm:text-sm">{modelServiceLabels[name]}</span><span className="mt-1 hidden truncate text-[11px] text-ink-muted sm:block">{enabled ? item.model || "待配置" : "未启用"}</span></span>
              </button>;
            })}
          </nav>
          <section className="overflow-hidden rounded-2xl border border-line bg-surface shadow-sm">
            <div className="flex items-start justify-between gap-4 border-b border-line p-5 sm:p-6">
              <div><h2 className="flex items-center gap-2 text-base font-semibold"><Icon className="size-5 text-accent" />{modelServiceLabels[section]}</h2><p className="mt-2 max-w-xl text-xs leading-6 text-ink-muted">{descriptions[section]}</p></div>
              {section !== "chat" && <label className="inline-flex shrink-0 items-center gap-2 text-xs text-ink-muted"><input type="checkbox" aria-label={`启用${modelServiceLabels[section]}`} disabled={busy} checked={draft.services[section].enabled} onChange={event => update(section, { enabled: event.target.checked })} />启用</label>}
            </div>
            <fieldset disabled={busy} className="space-y-5 p-5 sm:p-6">
              {section === "chat" && <Field label="接口协议" hint="协议与模型提供商相互独立；兼容服务可使用自定义地址。"><select aria-label="接口协议" className={fieldClass} value={draft.services.chat.protocol} onChange={event => {
                const protocol = event.target.value as ModelSettingsInput["services"]["chat"]["protocol"];
                update("chat", { protocol, base_url: protocol === "anthropic" ? "https://api.anthropic.com" : "https://api.openai.com/v1", api_key: "", model: "", reasoning_effort: "default", reasoning_levels: [], input_modalities: null });
              }}><option value="anthropic">Anthropic Messages</option><option value="openai_chat">OpenAI Chat Completions（兼容接口）</option><option value="openai_responses">OpenAI Responses</option></select></Field>}
              {section !== "chat" && <p className="rounded-xl bg-canvas px-3 py-2 text-xs text-ink-muted">OpenAI 兼容接口 · {section === "speech" ? "/audio/transcriptions" : "/embeddings"}</p>}
              <Field label="服务地址" hint={section === "chat" && draft.services.chat.protocol === "anthropic" ? "例如 https://api.anthropic.com；代理服务请填写其 Messages API 前缀。" : "包含 API 前缀，例如 https://api.openai.com/v1 或本地兼容服务的地址。"}>
                <input aria-label={`${modelServiceLabels[section]}服务地址`} className={fieldClass} value={service?.base_url ?? ""} placeholder="https://api.example.com/v1" onChange={event => update(section, { base_url: event.target.value.trim() })} spellCheck={false} />
              </Field>
              <Field label="API Key" hint={service?.api_key === "" ? "保存时将清除这一服务的密钥。本地免认证服务可留空。" : storedKey ? "已保存密钥。留空保持；更换服务地址后请重新填写或清除。" : "每种能力可使用独立密钥。本地免认证服务可留空。"}>
                <div className="flex items-center gap-2"><KeyRound className="size-4 shrink-0 text-ink-faint" /><input aria-label={`${modelServiceLabels[section]}API Key`} type="password" autoComplete="new-password" className={fieldClass} value={service?.api_key ?? ""} placeholder={storedKey && service?.api_key === null ? "已配置 · 留空保持" : "输入 API Key"} onChange={event => update(section, { api_key: event.target.value || null })} spellCheck={false} />
                  <button type="button" onClick={() => update(section, { api_key: "" })} className="shrink-0 rounded-lg border border-line px-3 py-2 text-xs text-ink-muted">清除</button></div>
              </Field>
              <Field label="模型名称" hint="可从服务获取列表，也可直接填写完整模型 ID。列表连接成功不代表该模型支持所有输入类型。">
                <div className="flex flex-col gap-2 sm:flex-row"><input aria-label={`${modelServiceLabels[section]}模型名称`} list={`models-${section}`} className={fieldClass} value={service?.model ?? ""} placeholder={section === "chat" ? "例如：你的对话模型 ID" : section === "speech" ? "例如：你的语音识别模型 ID" : "例如：你的 embedding 模型 ID"} onChange={event => update(section, { model: event.target.value })} spellCheck={false} />
                  <button type="button" disabled={busy || !service?.base_url} onClick={() => void discover(section)} className="inline-flex shrink-0 items-center justify-center gap-2 rounded-xl border border-line px-3 py-2 text-xs text-ink-muted hover:bg-surface-muted disabled:opacity-40">{discovering === section ? <Spinner className="size-3.5" /> : <RefreshCw className="size-3.5" />}获取模型列表</button></div>
                <datalist id={`models-${section}`}>{models[section]?.map(model => <option key={model} value={model} />)}</datalist>
              </Field>
              {discoveryStatus[section] && <p role="status" className="text-xs leading-5 text-ink-muted">{discoveryStatus[section]}</p>}
              <div className="rounded-xl border border-line bg-canvas p-4">
                <button type="button" disabled={busy || !service?.base_url || !service?.model.trim()} onClick={() => void testConnection(section)} className="inline-flex items-center gap-2 rounded-xl bg-accent px-4 py-2.5 text-xs font-semibold text-white hover:bg-accent-strong disabled:opacity-40">{testing === section ? <Spinner className="size-4" /> : <PlugZap className="size-4" />}{testing === section ? "正在测试…" : "测试连接"}</button>
                <p className="mt-2 text-[11px] leading-5 text-ink-muted">{!service?.model.trim() ? "先填写模型名称。" : ""}使用当前填写的配置实际调用模型，无需先保存。{section === "speech" ? "发送 1 秒静音样本。" : section === "embedding" ? "生成一条测试向量并校验维度。" : "发送一条简短测试消息。"}可能产生少量费用。</p>
              </div>
              {connection[section] && <p role="status" className={cx("rounded-xl border px-3 py-2 text-xs leading-5", connection[section]?.ok ? "border-success/20 bg-success/5 text-success" : "border-danger/20 bg-danger/5 text-danger")}>{connection[section]?.message}</p>}
              {section === "chat" && <>
                <div className="grid items-end gap-4 sm:grid-cols-2"><Field label="输出 Token 上限"><input aria-label="输出 Token 上限" type="number" min={1} max={2000000} className={fieldClass} value={draft.services.chat.max_tokens} onChange={event => update("chat", { max_tokens: Number(event.target.value) })} /></Field><label className="flex h-10 items-center gap-2 text-sm text-ink-muted"><input type="checkbox" checked={draft.services.chat.stream} onChange={event => update("chat", { stream: event.target.checked })} />流式显示回答</label></div>
                <details className="rounded-xl border border-line p-4"><summary className="cursor-pointer text-xs font-medium text-ink-muted">高级配置 · 输入能力与推理</summary><div className="mt-4 space-y-4">
                  <label className="flex items-center gap-2 text-xs text-ink-muted"><input type="checkbox" checked={draft.services.chat.input_modalities !== null} onChange={event => update("chat", { input_modalities: event.target.checked ? ["text"] : null })} />明确限制模型输入能力</label>
                  {draft.services.chat.input_modalities !== null && <div className="flex flex-wrap gap-4">{Object.entries({ text: "文字", image: "图片", document: "PDF", audio: "原生音频" }).map(([key, label]) => <label key={key} className="flex items-center gap-1.5 text-xs text-ink-muted"><input type="checkbox" checked={draft.services.chat.input_modalities?.includes(key)} disabled={key === "text" || key === "audio" && draft.services.chat.protocol !== "openai_chat"} onChange={event => update("chat", { input_modalities: event.target.checked ? [...(draft.services.chat.input_modalities ?? []), key] : draft.services.chat.input_modalities?.filter(item => item !== key) })} />{label}</label>)}</div>}
                  {draft.services.chat.protocol !== "anthropic" && <Field label="支持的推理等级" hint="逗号分隔，仅填写服务文档确认支持的值；不确定可留空使用默认。"><input aria-label="支持的推理等级" className={fieldClass} value={draft.services.chat.reasoning_levels.join(",")} placeholder="low,medium,high" onChange={event => update("chat", { reasoning_levels: event.target.value.split(",").map(value => value.trim()) })} /></Field>}
                  <Field label="默认推理等级"><select aria-label="默认推理等级" className={fieldClass} value={draft.services.chat.reasoning_effort} onChange={event => update("chat", { reasoning_effort: event.target.value })}>{Array.from(new Set(["default", draft.services.chat.reasoning_effort, ...draft.services.chat.reasoning_levels])).filter(Boolean).map(level => <option key={level} value={level}>{level === "default" ? "服务商默认" : level}</option>)}</select></Field>
                  {draft.services.chat.protocol === "openai_chat" && <Field label="输出上限参数"><select aria-label="输出上限参数" className={fieldClass} value={draft.services.chat.token_parameter} onChange={event => update("chat", { token_parameter: event.target.value as "max_tokens" | "max_completion_tokens" })}><option value="max_completion_tokens">max_completion_tokens</option><option value="max_tokens">max_tokens（旧兼容服务）</option></select></Field>}
                </div></details>
              </>}
              {section === "speech" && <div className="grid gap-4 sm:grid-cols-2"><Field label="语言提示" hint="留空自动识别；中文可填 zh。"><input aria-label="语音识别语言" className={fieldClass} value={draft.services.speech.language} placeholder="自动识别" onChange={event => update("speech", { language: event.target.value })} /></Field><Field label="超时（秒）"><input aria-label="超时（秒）" type="number" min={1} max={600} className={fieldClass} value={draft.services.speech.timeout_seconds} onChange={event => update("speech", { timeout_seconds: Number(event.target.value) })} /></Field></div>}
              {section === "embedding" && <div className="grid gap-4 sm:grid-cols-3"><Field label="向量维度" hint="须与服务返回值一致。"><input aria-label="向量维度" type="number" min={0} max={65536} className={fieldClass} value={draft.services.embedding.dimensions} onChange={event => update("embedding", { dimensions: Number(event.target.value) })} /></Field><Field label="每批文本数量"><input aria-label="每批文本数量" type="number" min={1} max={2048} className={fieldClass} value={draft.services.embedding.batch_size} onChange={event => update("embedding", { batch_size: Number(event.target.value) })} /></Field><Field label="超时（秒）"><input aria-label="超时（秒）" type="number" min={1} max={600} className={fieldClass} value={draft.services.embedding.timeout_seconds} onChange={event => update("embedding", { timeout_seconds: Number(event.target.value) })} /></Field></div>}
            </fieldset>
          </section>
        </div>}
        <p className="mt-5 text-xs leading-6 text-ink-faint">配置保存在本机 CodeAgent 数据目录，优先于环境变量；密钥不会回显到页面。获取模型列表仅读取列表；测试连接会实际调用所选模型，不会保存或修改配置。</p>
      </div>
    </div>
    <footer className="shrink-0 border-t border-line bg-surface"><div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-4 sm:px-8">
      <div className="min-w-0 flex-1">{error ? <p role="alert" className="flex items-center gap-2 text-xs text-danger"><CircleAlert className="size-4 shrink-0" />{error}</p> : message ? <p role="status" className="flex items-center gap-2 text-xs text-success"><Check className="size-4 shrink-0" />{message}</p> : <p className="text-xs text-ink-muted">{dirty ? "有未保存的更改" : "有运行中、排队中或未结束的团队任务时，请待任务结束后保存。"}</p>}</div>
      <button type="button" disabled={busy} onClick={() => void load()} className="rounded-xl border border-line px-4 py-2.5 text-xs text-ink-muted disabled:opacity-40">重新读取</button>
      <button type="button" disabled={busy || !draft || !dirty} onClick={() => void save()} className="inline-flex items-center gap-2 rounded-xl bg-accent px-5 py-2.5 text-xs font-semibold text-white shadow-sm hover:bg-accent-strong disabled:opacity-40">{saving ? <Spinner className="size-4 text-white" /> : <Save className="size-4" />}保存并应用</button>
    </div></footer>
  </main>;
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <div><div className="mb-2 text-xs font-medium text-ink">{label}</div>{children}{hint && <p className="mt-1.5 text-[11px] leading-5 text-ink-faint">{hint}</p>}</div>;
}
