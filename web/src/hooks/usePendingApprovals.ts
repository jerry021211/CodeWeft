import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const approvalsKey = (conversationId: string) => ["conversations", conversationId, "approvals"] as const;

export function pendingApprovalOptions(conversationId?: string) {
  return {
    queryKey: approvalsKey(conversationId ?? ""),
    queryFn: ({ signal }: { signal: AbortSignal }) => api.listPendingApprovals(conversationId!, signal),
    enabled: Boolean(conversationId),
    // Teammates can request permission after the planning SSE has closed, or
    // while a newer Lead Run is selected. Query the whole conversation.
    refetchInterval: 1000,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: "always" as const,
    retry: 1,
  };
}

export function usePendingApprovals(conversationId?: string) {
  return useQuery(pendingApprovalOptions(conversationId));
}
