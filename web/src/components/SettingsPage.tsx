import { useEffect, useState, type ReactNode } from "react";
import { Archive, ArrowLeft, Bot, Check, ChevronRight, Monitor, Moon, Plug, Settings2, Sun } from "lucide-react";
import { cx } from "@/lib/utils";
import type { Conversation } from "@/types/api";
import { ModelSettingsPage } from "@/components/ModelSettingsPage";
import { ArchivedConversationsPanel } from "@/components/ArchivedConversationsPanel";

const sections = [
  { id: "general", label: "通用", icon: Settings2, description: "调整工作台的外观和使用偏好。" },
  { id: "models", label: "模型与能力", icon: Bot, description: "配置对话模型、语音识别和向量检索。" },
  { id: "mcp", label: "MCP 插件", icon: Plug, description: "管理项目使用的外部工具与服务。" },
  { id: "archived", label: "归档会话", icon: Archive, description: "查看已归档的聊天记录，或将会话恢复到原项目。" },
] as const;
export type SettingsSection = typeof sections[number]["id"];
export function settingsSectionFromHash(hash: string): SettingsSection | undefined {
  if (hash === "#settings" || hash.startsWith("#settings/")) {
    return sections.find(section => hash === `#settings/${section.id}`)?.id ?? "general";
  }
  return undefined;
}

export function SettingsPage({ section, onClose, theme, onThemeChange, onSaved, mcpPanel, onRestored, onDelete }: {
  section: SettingsSection;
  onClose: () => void;
  theme: "system" | "light" | "dark";
  onThemeChange: (theme: "system" | "light" | "dark") => void;
  onSaved: () => void;
  mcpPanel: ReactNode;
  onRestored: (conversation: Conversation, open: boolean) => void;
  onDelete: (conversation: Conversation) => Promise<unknown>;
}) {
  // Keep model drafts while navigating between settings categories.
  const [modelsVisited, setModelsVisited] = useState(section === "models");
  useEffect(() => { if (section === "models") setModelsVisited(true); }, [section]);
  const current = sections.find(item => item.id === section)!;
  return <div className="flex h-dvh min-h-0 flex-col bg-canvas text-ink md:flex-row">
    <aside className="flex shrink-0 flex-col border-b border-line bg-surface-muted/50 md:w-56 md:border-b-0 md:border-r lg:w-64">
      <div className="px-4 pb-4 pt-5 md:px-5 md:pt-7">
        <button type="button" onClick={onClose} className="flex items-center gap-2 rounded-lg px-2 py-2 text-sm text-ink-muted transition hover:bg-surface-strong hover:text-ink"><ArrowLeft className="size-4" />返回对话</button>
        <div className="mt-5 hidden px-2 text-lg font-semibold tracking-tight md:block">设置</div>
      </div>
      <nav aria-label="设置分类" className="grid grid-cols-2 gap-1 px-3 pb-3 sm:flex sm:overflow-x-auto md:flex-col md:px-4">{sections.map(item => <a key={item.id} href={`#settings/${item.id}`} aria-current={section === item.id ? "page" : undefined} className={cx("flex shrink-0 items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition", section === item.id ? "bg-surface-strong font-medium text-ink" : "text-ink-muted hover:bg-surface-strong/60 hover:text-ink")}><item.icon className="size-4" />{item.label}</a>)}</nav>
      <p className="mt-auto hidden px-7 py-6 text-xs text-ink-faint md:block">CodeAgent · 本地工作台</p>
    </aside>
    <main className="min-h-0 min-w-0 flex-1 overflow-y-auto" aria-label="设置内容">
      <div className="mx-auto max-w-4xl px-5 py-7 sm:px-8 md:py-12 lg:px-12">
        <header className="mb-8"><h1 className="text-2xl font-semibold tracking-tight">{current.label}</h1><p className="mt-2 text-sm leading-6 text-ink-muted">{current.description}</p></header>
        {section === "general" && <div className="space-y-8">
          <section><h2 className="mb-3 text-sm font-semibold">外观</h2><div className="rounded-2xl border border-line bg-surface p-5"><h3 className="text-sm font-medium">主题</h3><p className="mt-1 text-xs text-ink-muted">选择浅色、深色，或跟随系统自动切换。</p><div className="mt-5 grid grid-cols-3 gap-3">{([
            { value: "system", label: "跟随系统", icon: Monitor }, { value: "light", label: "浅色", icon: Sun }, { value: "dark", label: "深色", icon: Moon },
          ] as const).map(item => <button key={item.value} type="button" aria-pressed={theme === item.value} onClick={() => onThemeChange(item.value)} className={cx("relative flex flex-col items-center gap-3 rounded-xl border px-2 py-5 text-xs transition sm:text-sm", theme === item.value ? "border-accent bg-accent/5 text-accent" : "border-line text-ink-muted hover:bg-surface-muted")}><item.icon className="size-6" />{item.label}{theme === item.value && <Check className="absolute right-2 top-2 size-3.5" />}</button>)}</div></div></section>
          <section><h2 className="mb-3 text-sm font-semibold">会话管理</h2><a href="#settings/archived" className="flex items-center gap-4 rounded-2xl border border-line bg-surface p-5 transition hover:bg-surface-muted"><Archive className="size-5 text-ink-muted" /><span className="flex-1"><span className="block text-sm font-medium">归档会话</span><span className="mt-1 block text-xs leading-5 text-ink-muted">查找历史记录，恢复会话或永久删除。</span></span><ChevronRight className="size-4 text-ink-faint" /></a></section>
          <p className="text-xs leading-6 text-ink-faint">外观设置自动保存在当前浏览器。聊天记录和项目配置保存在本机。</p>
        </div>}
        {(modelsVisited || section === "models") && <div hidden={section !== "models"}><ModelSettingsPage embedded onClose={onClose} onSaved={onSaved} /></div>}
        {section === "mcp" && mcpPanel}
        {section === "archived" && <ArchivedConversationsPanel onRestored={onRestored} onDelete={onDelete} />}
      </div>
    </main>
  </div>;
}
