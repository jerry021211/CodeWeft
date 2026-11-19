import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { request } from "@/lib/api";
import { WorkbenchAccess } from "./WorkbenchAccess";

export function MobileLinkPanel() {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, fail] = useState("");
  const [copied, setCopied] = useState(false);
  const state = useQuery({
    queryKey: ["mobile-link"], queryFn: () => request<{paired:boolean;remote?:boolean}>("/mobile/status"), refetchInterval: 5000,
  });
  useEffect(() => {
    if (!code) return;
    const timer = setTimeout(() => setCode(""), 300000);
    return () => clearTimeout(timer);
  }, [code]);

  async function change(action: "invite" | "revoke") {
    setBusy(true); fail(""); setCopied(false);
    try {
      const reply = await request<{code?:string}>("/mobile/" + action, {method:"POST",body:"{}"});
      setCode(reply.code ?? "");
      void state.refetch();
    } catch (e) { fail(e instanceof Error ? e.message : "操作失败"); } finally { setBusy(false); }
  }
  if (state.data?.remote) return <p className="rounded-2xl border border-line bg-surface p-5 text-sm">此手机已连接完整工作台。在电脑上可以更换配对手机或撤销授权。</p>;
  return <div className="space-y-5"><section className="space-y-4 rounded-2xl border border-line bg-surface p-5">
    <h2 className="font-semibold">{state.data?.paired ? "手机已配对" : "配对 iPhone"}</h2>
    <p className="text-sm text-ink-muted">手机和电脑连接同一 Wi-Fi，在 Expo Go 的聊天顶部点「切换电脑」，粘贴一次性配对码。配对后可查看本机工作台的项目和会话，发送只读问题。</p>
    <button disabled={busy} onClick={() => void change("invite")} className="rounded-lg border border-line px-3 py-2">生成配对码</button>
    {code && <div className="space-y-2"><p className="text-xs">配对码 5 分钟有效，仅供自己的手机使用。</p><code className="block select-all break-all">{code}</code>
      <button onClick={() => { void navigator.clipboard.writeText(code).then(() => setCopied(true)).catch(() => fail("请选择配对码并手动复制")); }}>{copied ? "已复制" : "复制配对码"}</button></div>}
    {(code || state.data?.paired) && <button disabled={busy} onClick={() => void change("revoke")} className="block text-sm text-ink-muted">撤销手机连接和配对码</button>}
    {(error || state.isError) && <p role="alert">{error || "无法读取手机连接状态，请确认已启动新版工作台后端。"}</p>}
  </section><WorkbenchAccess paired={!!state.data?.paired} /></div>;
}
