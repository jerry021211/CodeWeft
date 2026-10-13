import {useEffect} from "react";
import {useQuery} from "@tanstack/react-query";
import type {RunViewState} from "@/store/runStore";
import {api} from "@/lib/api";
import {isRunActive} from "@/lib/utils";

export function useCollaborationStatus(conversationId: string | undefined, history: Record<string, RunViewState> | undefined, current?: RunViewState) {
  const runs = [...Object.values(history ?? {}), ...(current ? [current] : [])];
  const enabled = Boolean(conversationId && runs.some(run => run.events.some(event => event.type === "collaboration.snapshot")));
  const currentCollaborates = current?.events.some(event => event.type === "collaboration.snapshot");
  const query = useQuery({
    queryKey: ["collaboration-status", conversationId], enabled,
    queryFn: ({signal}) => api.collaborationStatuses(conversationId!, signal),
    staleTime: 5000, retry: 1,
    refetchInterval: enabled && currentCollaborates && current && isRunActive(current.status) ? 5000 : false,
  });
  // Covers terminal events, connection recovery and cached historical sessions.
  const refetch = query.refetch;
  useEffect(() => {if (enabled) void refetch();}, [enabled, conversationId, current?.status, current?.connection, refetch]);
  return {snapshots: query.data, error: query.isError, syncing: query.isFetching,
    retry: () => {void query.refetch();}};
}
