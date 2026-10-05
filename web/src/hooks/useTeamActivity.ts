import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { RunEvent } from "@/types/api";

/** Team workers keep writing after their planning run has completed. */
export function useTeamActivity(teamId: string, runId: string, active: boolean) {
  const [data, setData] = useState<{ key: string; events: RunEvent[]; error?: string; loading: boolean }>({ key: teamId, events: [], loading: true });
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    let cursor = 0;
    const events: RunEvent[] = [];
    setData({ key: teamId, events: [], loading: true });
    async function poll() {
      try {
        const page = await api.getRunActivityPage(runId, cursor, controller.signal);
        if (disposed) return;
        for (const event of page.events) if (event.seq > cursor) events.push(event);
        cursor = Math.max(cursor, ...page.events.map(event => event.seq));
        setData({ key: teamId, events: [...events], loading: page.next_after != null });
        if (page.next_after != null || active) timer = setTimeout(poll, page.next_after != null ? 0 : 1500);
      } catch (cause) {
        if (disposed) return;
        setData({ key: teamId, events: [...events], loading: false, error: cause instanceof Error ? cause.message : "读取成员工作记录失败" });
        timer = setTimeout(poll, 3000);
      }
    }
    void poll();
    return () => { disposed = true; controller.abort(); clearTimeout(timer); };
  }, [teamId, runId, active]);
  return data.key === teamId ? data : { events: [], loading: true, error: undefined };
}
