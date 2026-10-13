import type { PersonalTodoPlan, RunStatus, TeamSnapshot } from "@/types/api";
import type { RunViewState } from "@/store/runStore";

export type TodoExecutionStatus = RunStatus | "review" | "paused" | "idle";

/** A recorded in_progress step is not evidence of a currently running Agent. */
export function personalTodoStatus(plan?: PersonalTodoPlan, run?: RunViewState, team?: TeamSnapshot): TodoExecutionStatus {
  if (!plan) return "idle";
  const session = team?.sessions.find(item => item.id === plan.owner_id);
  if (session && team) {
    if (["cancelled", "failed", "completed"].includes(team.team.state)) return team.team.state as RunStatus;
    if (["paused", "pausing"].includes(team.team.state)) return "paused";
    if (["failed", "lost", "suspect"].includes(session.state)) return "interrupted";
    return session.state === "work" ? "running" : session.state === "starting" ? "queued" : "idle";
  }
  if (run && plan.owner_id === "agent_root") {
    // A new Run has not adopted the old plan until it writes its own snapshot.
    if (run.runId !== plan.run_id && run.status === "running") return "review";
    return run.status;
  }
  if (run?.runId === plan.run_id) {
    if (["cancelled", "failed", "interrupted", "cancelling"].includes(run.status)) return run.status;
    const member = run.agents[plan.agent_id];
    const status = member?.runtime_status ?? member?.status;
    if (status === "running") return "running";
    if (["completed", "failed", "cancelled", "interrupted", "queued", "waiting_approval"].includes(status ?? "")) return status as RunStatus;
    return "idle";
  }
  // A persisted Run status remains available after refresh, before SSE replay.
  return plan.run_status ?? "idle";
}
