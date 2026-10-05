import type { PlanDocument } from "@/types/api";

/** Keep submitted plans with their generating turn, including after execution starts. */
export function conversationPlans(plans: PlanDocument[] = []) {
  const byRun: Record<string, PlanDocument[]> = {};
  for (const plan of [...plans].sort((a, b) => a.revision - b.revision)) {
    if (plan.status === "draft" || !plan.run_id) continue;
    (byRun[plan.run_id] ??= []).push(plan);
  }
  return byRun;
}
