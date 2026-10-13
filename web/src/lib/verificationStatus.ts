import type { RunEvent } from "@/types/api";

export function verificationStatus(events: RunEvent[]) {
  const ordered = events.filter(event => !event.parent_agent_id).sort((a, b) => b.seq - a.seq);
  const status = ordered.find(event => event.type === "cvm.verification_status");
  if (!status) return undefined;
  const checks = Array.isArray(status.payload.checks) ? status.payload.checks.filter(
    (row): row is Record<string, unknown> => !!row && typeof row === "object",
  ) : [];
  const advice = ordered.find(event => event.type === "cvm.verification_advice");
  return { status, checks, advice };
}
