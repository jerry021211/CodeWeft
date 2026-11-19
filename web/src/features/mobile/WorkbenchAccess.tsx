import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { request } from "@/lib/api";

type Access = { enabled: boolean; origin: string | null; addresses: string[] };

export function WorkbenchAccess({ paired }: { paired: boolean }) {
  const state = useQuery({ queryKey: ["mobile-workbench"], queryFn: () => request<Access>("/mobile/workbench"), refetchInterval: 5000 });
  const [address, select] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, fail] = useState("");
  const selected = address || state.data?.addresses[0] || "";
  const active = state.data?.enabled;
  async function toggle() {
    setBusy(true); fail("");
    try {
      await request<Access>("/mobile/workbench", { method: active ? "DELETE" : "POST", body: JSON.stringify({address:selected}) });
      await state.refetch();
    } catch (e) { fail(e instanceof Error ? e.message : "无法更新工作台访问权限"); }
    finally { setBusy(false); }
  }
  return <section className="space-y-4 rounded-2xl border border-line bg-surface p-5">
    <h2 className="font-semibold">完整工作台</h2>
    <p className="text-sm leading-6 text-ink-muted">授权当前配对手机使用桌面工作台的项目、执行工具、审批和设置。电脑上的权限规则同样适用。</p>
    {active ? <div className="space-y-3 text-sm"><p>已开启 · {state.data?.origin}</p>
      <a className="text-accent underline" href="/api/mobile/workbench/certificate" download>下载此电脑的公开证书</a>
      <p className="leading-6 text-ink-muted">首次使用时，将证书传到 iPhone 并安装，在「设置 → 通用 → 关于本机 → 证书信任设置」信任该 CodeWeft 证书。然后在手机 App 的电脑页点「完整工作台」。</p></div> :
      <label className="block text-sm">电脑的 Wi-Fi 地址<select className="ml-3 rounded-lg border border-line bg-surface px-2 py-1" value={selected} onChange={e => select(e.target.value)}>
        {state.data?.addresses.map(ip => <option key={ip}>{ip}</option>)}</select></label>}
    <button className="rounded-lg border border-line px-3 py-2 text-sm" disabled={busy || (!active && (!paired || !selected))} onClick={() => void toggle()}>
      {active ? "关闭完整工作台" : paired ? "授权此手机并开启" : "请先配对手机"}</button>
    {(error || state.isError) && <p role="alert">{error || "请启动新版工作台后端"}</p>}
  </section>;
}
