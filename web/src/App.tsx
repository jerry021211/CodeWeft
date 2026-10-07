import { useEffect, useMemo, useState } from "react";
import { useMutation, useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, X } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { cx, isRunActive } from "@/lib/utils";
import type { Attachment, Approval, ApprovalDecision, Conversation, McpConfig, Message, SaveMcpServer, TaskResource } from "@/types/api";
import { ConversationSidebar } from "@/components/ConversationSidebar";
import { ChatWorkspace } from "@/components/ChatWorkspace";
import { PlanPreview } from "@/components/PlanPreview";
import type { PlanDocument } from "@/types/api";
import { InspectorPanel } from "@/components/InspectorPanel";
import { WorkspacePicker } from "@/components/WorkspacePicker";
import { McpConfigModal } from "@/components/McpConfigModal";
import { SettingsPage, settingsSectionFromHash } from "@/components/SettingsPage";
import { archivedConversationsKey } from "@/components/ArchivedConversationsPanel";
import { workspaceKey } from "@/lib/conversationGroups";
import { useSidebarStore } from "@/store/sidebarStore";
import { useRunEvents } from "@/hooks/useRunEvents";
import { approvalsKey, usePendingApprovals } from "@/hooks/usePendingApprovals";
import { useConversationActivity } from "@/hooks/useConversationActivity";
import { useRunStore } from "@/store/runStore";

type ThemeMode = "system" | "light" | "dark";

const conversationsKey = ["conversations"] as const;
const messagesKey = (conversationId: string) => ["conversations", conversationId, "messages"] as const;
const tasksKey = (taskListId: string) => ["task-lists", taskListId, "tasks"] as const;
const teamsKey = (conversationId: string) => ["teams", conversationId] as const;

type TeamCommand =
  | { kind: "team-plan"; revision: number; decision: "approve" | "reject"; reason: string }
  | { kind: "candidate-approval"; candidateId: string; decision: "approve" | "reject"; reason: string }
  | { kind: "resume-attempt"; attemptId: string; reason: string; acknowledgeUnknownResult: boolean }
  | { kind: "cancel"; reason: string }
  | { kind: "pause" | "resume"; reason: string }
  | { kind: "integration"; targetRef: string }
  | { kind: "resolve-integration"; integrationId: string; action: "retry" | "repair" | "resume"; reason: string }
  | { kind: "cleanup"; worktreeId: string };

export default function App() {
  const queryClient = useQueryClient();
  const [settingsSection, setSettingsSection] = useState(() => settingsSectionFromHash(window.location.hash));
  const settingsOpen = Boolean(settingsSection);
  useEffect(() => {
    const changed = () => setSettingsSection(settingsSectionFromHash(window.location.hash));
    const shortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key === ",") {
        event.preventDefault();
        window.location.hash = "settings/general";
      }
    };
    window.addEventListener("hashchange", changed);
    window.addEventListener("keydown", shortcut);
    return () => { window.removeEventListener("hashchange", changed); window.removeEventListener("keydown", shortcut); };
  }, []);
  const openSettings = () => { setLeftOpen(false); window.location.hash = "settings/general"; };
  const openModelSettings = () => { setLeftOpen(false); window.location.hash = "settings/models"; };
  const [selectedId, setSelectedId] = useState<string | undefined>(() => localStorage.getItem("codeagent.conversation") || undefined);
  const [search, setSearch] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [attachmentDrafts, setAttachmentDrafts] = useState<Record<string, Attachment[]>>({});
  const attachments = selectedId ? attachmentDrafts[selectedId] ?? [] : [];
  const setAttachments = (items: Attachment[]) => {
    if (selectedId) setAttachmentDrafts(current => ({ ...current, [selectedId]: items }));
  };
  const draft = selectedId ? drafts[selectedId] ?? "" : "";
  const setDraft = (value: string) => {
    if (selectedId) setDrafts((current) => ({ ...current, [selectedId]: value }));
  };
  const sendingConversations = useMutationState({
    filters: { mutationKey: ["send-run"], status: "pending" },
    select: (mutation) => (mutation.state.variables as { conversationId: string }).conversationId,
  });
  const cancellingRuns = useMutationState({
    filters: { mutationKey: ["cancel-run"], status: "pending" },
    select: (mutation) => mutation.state.variables as string,
  });
  const decidingRuns = useMutationState({
    filters: { mutationKey: ["decide-approval"], status: "pending" },
    select: (mutation) => (mutation.state.variables as { targetRunId: string }).targetRunId,
  });
  const sending = Boolean(selectedId && sendingConversations.includes(selectedId));
  const [readOnlyChoices, setReadOnlyChoices] = useState<Record<string, boolean>>({});
  const [teamChoices, setTeamChoices] = useState<Record<string, boolean>>({});
  const [planChoices, setPlanChoices] = useState<Record<string, boolean>>({});
  const [webSearchChoices, setWebSearchChoices] = useState<Record<string, boolean>>({});
  const [reasoningChoices, setReasoningChoices] = useState<Record<string, string>>({});
  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(false);
  const [runIds, setRunIds] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string>();
  const [workspacePickerOpen, setWorkspacePickerOpen] = useState(false);
  const [workspacePath, setWorkspacePath] = useState<string>();
  const [workspaceSearch, setWorkspaceSearch] = useState("");
  const mcpOpen = settingsSection === "mcp";
  const [mcpWorkspaceChoice, setMcpWorkspaceChoice] = useState<string>();
  const [mcpMessage, setMcpMessage] = useState<string>();
  const { theme, cycleTheme, setTheme } = useTheme();

  const conversationsQuery = useQuery({
    queryKey: conversationsKey,
    queryFn: () => api.listConversations({ archived: false }),
    refetchInterval: (query) => query.state.data?.some((item) => isRunActive(item.run_status)) ? 2_000 : 15_000,
  });
  const conversations = conversationsQuery.data ?? [];
  const filteredConversations = useMemo(() => {
    const term = search.trim().toLocaleLowerCase("zh-CN");
    if (!term) return conversations;
    return conversations.filter((item) => `${item.title} ${item.last_message ?? ""}`.toLocaleLowerCase("zh-CN").includes(term));
  }, [conversations, search]);

  useEffect(() => {
    if (selectedId && conversations.some((item) => item.id === selectedId)) return;
    const first = conversations[0];
    if (first) setSelectedId(first.id);
    else if (!conversationsQuery.isLoading) setSelectedId(undefined);
  }, [conversations, conversationsQuery.isLoading, selectedId]);

  useEffect(() => {
    if (selectedId) localStorage.setItem("codeagent.conversation", selectedId);
    else localStorage.removeItem("codeagent.conversation");
  }, [selectedId]);

  const conversationQuery = useQuery({
    queryKey: ["conversations", selectedId],
    queryFn: () => api.getConversation(selectedId!),
    enabled: Boolean(selectedId),
  });
  const selectedConversation = conversations.find((item) => item.id === selectedId) ?? conversationQuery.data;
  const taskListId = selectedConversation?.active_task_list_id ?? undefined;
  const messagesQuery = useQuery({
    queryKey: messagesKey(selectedId ?? ""),
    queryFn: () => api.listMessages(selectedId!),
    enabled: Boolean(selectedId),
  });
  const runtimeQuery = useQuery({ queryKey: ["runtime-config"], queryFn: api.getRuntimeConfig, staleTime: 60_000 });
  const lastUserMessage = [...(messagesQuery.data ?? [])].reverse().find((message) => message.role === "user");
  const readOnly = readOnlyChoices[selectedId ?? ""] ?? lastUserMessage?.metadata?.read_only ?? (lastUserMessage?.metadata?.mode === "discuss");
  const selectReadOnly = (value: boolean) => {
    if (selectedId) setReadOnlyChoices((current) => ({ ...current, [selectedId]: value }));
  };
  const teamEnabled = Boolean(runtimeQuery.data?.features?.agent_team);
  const plansQuery = useQuery({
    queryKey: ["plans", selectedId], queryFn: () => api.getPlans(selectedId!), enabled: Boolean(selectedId),
    refetchInterval: query => query.state.data?.state.mode === "planning" ? 1000 : 5000,
  });
  const planningActive = plansQuery.data?.state.mode === "planning";
  const teamsQuery = useQuery({
    queryKey: teamsKey(selectedId ?? ""),
    queryFn: () => api.listTeams(selectedId!),
    enabled: teamEnabled && Boolean(selectedId),
    refetchInterval: query => teamEnabled && query.state.data?.some(item => !["completed", "failed", "cancelled", "closed_with_unmerged_candidates"].includes(item.team.state)) ? 1500 : teamEnabled ? 5000 : false,
  });
  const team = useMemo(() => {
    const teams = teamsQuery.data ?? [];
    const terminal = new Set(["completed", "failed", "cancelled", "closed_with_unmerged_candidates"]);
    return teams.find((item) => !terminal.has(item.team.state))
      ?? [...teams].sort((left, right) => right.team.updated_at.localeCompare(left.team.updated_at))[0];
  }, [teamsQuery.data]);
  const teamLeadActive = Boolean(team && !["completed", "failed", "cancelled", "closed_with_unmerged_candidates"].includes(team.team.state));
  const useTeam = teamEnabled && (teamLeadActive || (!readOnly && (planningActive
    ? plansQuery.data?.state.target === "team" : Boolean(teamChoices[selectedId ?? ""]))));
  const planMode = !teamLeadActive && (planningActive || useTeam || Boolean(planChoices[selectedId ?? ""]));
  const planDecision = useMutation({
    mutationFn: ({ conversationId, plan, decision }: { conversationId: string; plan: PlanDocument; decision: "approve" | "reject" | "withdraw" | "restore" }) => api.decidePlan(conversationId, plan, decision),
    onSuccess: (plan, variables) => {
      if (variables.decision === "restore") {
        setDrafts(current => current[variables.conversationId] === "请修改方案："
          ? { ...current, [variables.conversationId]: "" } : current);
      }
      if (plan.status === "started") {
        setPlanChoices(current => ({ ...current, [variables.conversationId]: false }));
        if (plan.execution_run_id) setRunIds(current => ({ ...current, [variables.conversationId]: plan.execution_run_id! }));
      }
      void queryClient.invalidateQueries({ queryKey: ["plans", variables.conversationId] });
      void queryClient.invalidateQueries({ queryKey: teamsKey(variables.conversationId) });
      void queryClient.invalidateQueries({ queryKey: messagesKey(variables.conversationId) });
    },
  });
  const exitPlan = useMutation({
    mutationFn: (conversationId: string) => api.exitPlanning(conversationId),
    onSuccess: (_, conversationId) => {
      setPlanChoices(current => ({ ...current, [conversationId]: false }));
      setTeamChoices(current => ({ ...current, [conversationId]: false }));
      void queryClient.invalidateQueries({ queryKey: ["plans", conversationId] });
    },
    onError: error => setNotice(errorMessage(error)),
  });
  const selectPlanMode = (enabled: boolean) => {
    if (!selectedId) return;
    if (!enabled && planningActive) exitPlan.mutate(selectedId);
    else setPlanChoices(current => ({ ...current, [selectedId]: enabled }));
  };
  const selectTeam = (enabled: boolean) => {
    if (selectedId) setTeamChoices((current) => ({ ...current, [selectedId]: enabled }));
  };
  const webSearchAvailable = Boolean(runtimeQuery.data?.features?.web_search);
  const webSearch = webSearchAvailable && !useTeam && !teamLeadActive && (webSearchChoices[selectedId ?? ""]
    ?? lastUserMessage?.metadata?.web_search_enabled ?? runtimeQuery.data?.features?.web_search_default ?? false);
  const selectWebSearch = (enabled: boolean) => {
    if (selectedId) setWebSearchChoices((current) => ({ ...current, [selectedId]: enabled }));
  };
  const reasoningOptions = ["default", ...(runtimeQuery.data?.reasoning?.supports_disabled ? ["none"] : []),
    ...(runtimeQuery.data?.reasoning?.supported_levels ?? [])];
  const savedReasoning = reasoningChoices[selectedId ?? ""] ?? lastUserMessage?.metadata?.reasoning_effort
    ?? runtimeQuery.data?.reasoning_effort ?? "default";
  const reasoningEffort = typeof savedReasoning === "string" && reasoningOptions.includes(savedReasoning) ? savedReasoning : "default";
  const selectReasoning = (effort: string) => {
    if (selectedId) setReasoningChoices((current) => ({ ...current, [selectedId]: effort }));
  };

  const taskListQuery = useQuery({
    queryKey: ["task-lists", taskListId],
    queryFn: () => api.getTaskList(taskListId!),
    enabled: Boolean(taskListId),
  });
  const tasksQuery = useQuery({
    queryKey: tasksKey(taskListId ?? ""),
    queryFn: () => api.listTasks(taskListId!),
    enabled: Boolean(taskListId),
  });
  const activeRuntime = runtimeQuery.data
    ? { ...runtimeQuery.data, workspace: selectedConversation?.workspace ?? runtimeQuery.data.workspace }
    : runtimeQuery.data;
  const workspacesQuery = useQuery({
    queryKey: ["workspaces", workspacePath ?? "default", workspaceSearch],
    queryFn: () => api.listWorkspaces(workspacePath, workspaceSearch),
    enabled: workspacePickerOpen,
    retry: false,
  });
  const mcpWorkspace = mcpWorkspaceChoice ?? selectedConversation?.workspace ?? runtimeQuery.data?.workspace;
  const mcpWorkspaces = [...new Set([mcpWorkspace, runtimeQuery.data?.workspace, ...conversations.map(item => item.workspace)].filter((path): path is string => Boolean(path)))];
  const mcpQuery = useQuery({
    queryKey: ["mcp-servers", mcpWorkspace],
    queryFn: () => api.getMcpConfig(mcpWorkspace!),
    enabled: mcpOpen && Boolean(mcpWorkspace),
  });

  const runId = selectedId ? selectedConversation?.active_run_id ?? runIds[selectedId] ?? selectedConversation?.latest_run_id ?? undefined : undefined;
  const liveRun = useRunEvents(runId);
  const activityQuery = useConversationActivity(selectedId ?? undefined, messagesQuery.data ?? [], liveRun);
  const ensureRun = useRunStore((state) => state.ensureRun);
  const setRunStatus = useRunStore((state) => state.setRunStatus);
  const resolveApproval = useRunStore((state) => state.resolveApproval);
  const approvalsQuery = usePendingApprovals(selectedId);
  const pendingApproval = approvalsQuery.data?.find(item => item.status === "pending");

  useEffect(() => {
    if (!taskListId) return;
    const source = new EventSource(api.taskEventStreamUrl(taskListId), { withCredentials: true });
    const refresh = () => {
      void queryClient.invalidateQueries({ queryKey: tasksKey(taskListId) });
      void queryClient.invalidateQueries({ queryKey: ["task-lists", taskListId] });
    };
    source.addEventListener("task.created", refresh);
    source.addEventListener("task.updated", refresh);
    source.addEventListener("task.completed", refresh);
    return () => source.close();
  }, [queryClient, taskListId]);

  useEffect(() => {
    if (!team?.team.id || !selectedId) return;
    const source = new EventSource(api.teamEventStreamUrl(team.team.id), { withCredentials: true });
    const refresh = () => void queryClient.invalidateQueries({ queryKey: teamsKey(selectedId) });
    const eventTypes = [
      "team.plan.created", "team.plan.submitted", "team.plan.approved", "team.plan.rejected",
      "team.attempt.assigned", "team.attempt.cancel_requested",
      "team.attempt_plan.submitted", "team.attempt_plan.approved", "team.attempt_plan.rejected",
      "team.candidate.submitted", "team.candidate.accepted", "team.candidate.rework",
      "team.candidate.user_approved", "team.candidate.user_rejected",
      "team.candidate.committed", "team.candidate.validation_failed", "team.analysis.completed", "team.integration.verified",
      "team.session.state_changed", "team.session.suspect", "team.scope_violation",
      "team.worktree.bound", "team.worktree.cleaned", "team.cancelled",
    ];
    eventTypes.forEach((type) => source.addEventListener(type, refresh));
    return () => source.close();
  }, [queryClient, selectedId, team?.team.id]);

  useEffect(() => {
    if (!runId) return;
    let disposed = false;
    const initialSeq = useRunStore.getState().runs[runId]?.lastSeq ?? 0;
    void api.getRun(runId).then((run) => {
      if (disposed) return;
      ensureRun(run.id, run.status, run.queue_position);
      const current = useRunStore.getState().runs[run.id];
      // An HTTP snapshot must not overwrite newer progress/terminal SSE events.
      if (current && current.lastSeq === initialSeq && current.connection !== "closed") {
        setRunStatus(run.id, run.status, run.error ?? undefined);
      }
    }).catch(() => undefined);
    return () => { disposed = true; };
  }, [ensureRun, runId, setRunStatus]);

  useEffect(() => {
    if (!liveRun || isRunActive(liveRun.status)) return;
    void queryClient.invalidateQueries({ queryKey: conversationsKey });
    if (selectedId) void queryClient.invalidateQueries({ queryKey: messagesKey(selectedId) });
    if (taskListId) void queryClient.invalidateQueries({ queryKey: tasksKey(taskListId) });
  }, [liveRun?.status, liveRun?.runId, queryClient, selectedId, taskListId]);

  const createConversation = useMutation({
    mutationFn: (workspace: string) => api.createConversation({ workspace }),
    onSuccess: (conversation) => {
      queryClient.setQueryData<Conversation[]>(conversationsKey, (current = []) => [conversation, ...current.filter((item) => item.id !== conversation.id)]);
      setSelectedId(conversation.id);
      setSearch("");
      setLeftOpen(false);
      setWorkspacePickerOpen(false);
      setWorkspacePath(undefined);
      setWorkspaceSearch("");
    },
    onError: (error) => showError(error, setNotice),
  });

  const archiveConversation = useMutation({
    mutationFn: (conversation: Conversation) => api.updateConversation(conversation.id, { archived: true }),
    onSuccess: (_, conversation) => {
      queryClient.setQueryData<Conversation[]>(conversationsKey, (current = []) => current.filter((item) => item.id !== conversation.id));
      void queryClient.invalidateQueries({ queryKey: archivedConversationsKey });
      if (selectedId === conversation.id) setSelectedId(undefined);
    },
    onError: (error) => showError(error, setNotice),
  });

  const deleteConversation = useMutation({
    mutationFn: (conversation: Conversation) => api.deleteConversation(conversation.id),
    onSuccess: async (_, conversation) => {
      await queryClient.cancelQueries({ queryKey: conversationsKey });
      await queryClient.cancelQueries({ queryKey: teamsKey(conversation.id) });
      queryClient.setQueryData<Conversation[]>(conversationsKey, (current = []) => current.filter((item) => item.id !== conversation.id));
      setSelectedId((current) => current === conversation.id ? undefined : current);
      queryClient.removeQueries({ queryKey: ["conversations", conversation.id] });
      queryClient.removeQueries({ queryKey: teamsKey(conversation.id) });
      void queryClient.invalidateQueries({ queryKey: archivedConversationsKey });
      for (const setter of [setDrafts, setRunIds]) {
        setter((current) => {
          const next = { ...current };
          delete next[conversation.id];
          return next;
        });
      }
      setReadOnlyChoices((current) => {
        const next = { ...current };
        delete next[conversation.id];
        return next;
      });
    },
    onError: (error) => showError(error, setNotice),
  });
  const confirmDeleteConversation = (conversation: Conversation) => {
    if (sendingConversations.includes(conversation.id)) {
      setNotice("消息正在发送，请等待任务结束后再删除会话。");
      return;
    }
    if (window.confirm(`确定永久删除会话「${conversation.title || "新会话"}」？\n聊天记录和运行记录将被删除，无法恢复。工作区文件会保留。`)) {
      deleteConversation.mutate(conversation);
    }
  };

  const sendRun = useMutation({
    mutationKey: ["send-run"],
    mutationFn: async ({ conversationId, content, useTeam, readOnly: submittedReadOnly, webSearch, reasoningEffort, attachments, planMode: submittedPlanMode }: { attachments: Attachment[]; conversationId: string; content: string; useTeam: boolean; readOnly: boolean; webSearch: boolean; reasoningEffort: string; planMode?: boolean }) => {
      const result = await api.createRun(conversationId, content, useTeam, submittedReadOnly, webSearch, reasoningEffort, attachments, submittedPlanMode);
      return { ...result, content, conversationId, attachments };
    },
    onSuccess: (result) => {
      setAttachmentDrafts(current => current[result.conversationId] === result.attachments ? { ...current, [result.conversationId]: [] } : current);
      ensureRun(result.run_id, result.status, result.queue_position);
      setRunIds((current) => ({ ...current, [result.conversationId]: result.run_id }));
      setDrafts((current) => current[result.conversationId]?.trim() === result.content
        ? { ...current, [result.conversationId]: "" } : current);
      void queryClient.invalidateQueries({ queryKey: conversationsKey });
    },
    onError: (error) => showError(error, setNotice),
    onSettled: (_, __, variables) => {
      void queryClient.invalidateQueries({ queryKey: messagesKey(variables.conversationId) });
      void queryClient.invalidateQueries({ queryKey: teamsKey(variables.conversationId) });
    },
  });

  const cancelRun = useMutation({
    mutationKey: ["cancel-run"],
    mutationFn: (targetRunId: string) => api.cancelRun(targetRunId),
    onMutate: (targetRunId) => setRunStatus(targetRunId, "cancelling"),
    onError: (error, targetRunId) => {
      void api.getRun(targetRunId).then((run) => setRunStatus(targetRunId, run.status, run.error ?? undefined));
      showError(error, setNotice);
    },
  });

  const decideApproval = useMutation({
    mutationKey: ["decide-approval"],
    mutationFn: ({ targetRunId, approvalId, decision }: { conversationId: string; targetRunId: string; approvalId: string; decision: ApprovalDecision }) => api.decideApproval(targetRunId, approvalId, decision),
    onSuccess: async (approval, variables) => {
      resolveApproval(variables.targetRunId, approval.id, approval.status);
      await queryClient.cancelQueries({ queryKey: approvalsKey(variables.conversationId) });
      queryClient.setQueryData<Approval[]>(approvalsKey(variables.conversationId), current => current?.filter(item => item.id !== approval.id));
    },
    onError: (error) => showError(error, setNotice),
    onSettled: (_data, _error, variables) => queryClient.invalidateQueries({ queryKey: approvalsKey(variables.conversationId) }),
  });

  const createTask = useMutation({
    mutationFn: (input: { subject: string; description: string; activeForm?: string }) => {
      if (!taskListId) throw new Error("当前会话没有任务列表");
      return api.createTask(taskListId, input);
    },
    onSuccess: (resource) => {
      queryClient.setQueryData<TaskResource[]>(tasksKey(resource.taskListId), (current = []) => [...current, resource]);
    },
    onError: (error) => showError(error, setNotice),
  });

  const teamCommand = useMutation({
    mutationFn: async (command: TeamCommand) => {
      if (!team) throw new Error("当前会话没有 TeamRun");
      const teamId = team.team.id;
      switch (command.kind) {
        case "team-plan":
          await api.decideTeamPlan(teamId, command.revision, command.decision, command.reason);
          return;
        case "candidate-approval":
          await api.approveCandidate(teamId, command.candidateId, command.decision, command.reason);
          return;
        case "resume-attempt":
          await api.resumeAttempt(teamId, command.attemptId, command.reason, command.acknowledgeUnknownResult);
          return;
        case "cancel":
          await api.cancelTeam(teamId, command.reason);
          return;
        case "pause":
        case "resume":
          await api.controlTeam(teamId, command.kind, command.reason);
          return;
        case "integration":
          await api.verifyManualIntegration(teamId, command.targetRef);
          return;
        case "resolve-integration":
          await api.resolveTeamIntegration(teamId, command.integrationId, command.action, command.reason);
          return;
        case "cleanup":
          await api.disposeWorktree(teamId, command.worktreeId, "cleanup");
          return;
      }
    },
    onSuccess: () => {
      if (selectedId) void queryClient.invalidateQueries({ queryKey: teamsKey(selectedId) });
      if (taskListId) void queryClient.invalidateQueries({ queryKey: tasksKey(taskListId) });
    },
    onError: (error) => showError(error, setNotice),
  });

  const saveMcpServer = useMutation({
    mutationFn: (server: SaveMcpServer) => api.saveMcpServer(server),
    onSuccess: (config) => {
      queryClient.setQueryData<McpConfig>(["mcp-servers", config.workspace], config);
      setMcpMessage(config.restart_required ? "配置已保存。请重启 CodeAgent 后使用。" : "配置已保存，后续任务会加载新工具；当前任务继续使用原连接。");
    },
  });

  const deleteMcpServer = useMutation({
    mutationFn: ({ workspace, name }: { workspace: string; name: string }) => api.deleteMcpServer(workspace, name),
    onSuccess: (config) => {
      queryClient.setQueryData<McpConfig>(["mcp-servers", config.workspace], config);
      setMcpMessage(config.restart_required ? "配置已删除。请重启 CodeAgent。" : "配置已删除，后续任务使用新配置；当前任务继续使用原连接。");
    },
  });

  const continueTask = (task: TaskResource) => {
    if (readOnly) {
      setNotice("当前已开启只读保护。执行任务前请先关闭只读保护。");
      return;
    }
    if (!selectedId || sending || liveRun && isRunActive(liveRun.status)) return;
    sendRun.mutate({
      attachments: [],
      conversationId: selectedId,
      content: `继续处理 Task #${task.task.id}：${task.task.subject}。先读取 TaskGet，按 description 的完成条件执行，并及时用 TaskUpdate 更新状态。`,
      useTeam,
      readOnly,
      webSearch,
      reasoningEffort,
    });
  };

  const send = () => {
    const content = draft.trim();
    if (sending || messagesQuery.isLoading) return;
    if (!selectedId || (!content && !attachments.length) || liveRun && isRunActive(liveRun.status)) return;
    const optimistic: Message = {
      id: `optimistic:${Date.now()}`,
      conversation_id: selectedId,
      role: "user",
      content,
      created_at: new Date().toISOString(),
      status: "complete",
      metadata: { read_only: readOnly, web_search_enabled: webSearch, reasoning_effort: reasoningEffort, attachments: attachments.map(({ name, media_type }) => ({ name, media_type })) },
    };
    queryClient.setQueryData<Message[]>(messagesKey(selectedId), (current = []) => [...current, optimistic]);
    sendRun.mutate({ conversationId: selectedId, content, useTeam, readOnly, webSearch, reasoningEffort, attachments, planMode });
  };

  const teamPanelProps = {
    teamEnabled,
    team,
    teamLoading: teamsQuery.isLoading,
    teamBusy: teamCommand.isPending,
    teamError: teamsQuery.error ? errorMessage(teamsQuery.error) : teamCommand.error ? errorMessage(teamCommand.error) : undefined,
    onTeamPlan: (revision: number, decision: "approve" | "reject", reason: string) => teamCommand.mutate({ kind: "team-plan", revision, decision, reason }),
    onCandidateApproval: (candidateId: string, decision: "approve" | "reject", reason: string) => teamCommand.mutate({ kind: "candidate-approval", candidateId, decision, reason }),
    onResumeAttempt: (attemptId: string, reason: string, acknowledgeUnknownResult: boolean) => teamCommand.mutate({ kind: "resume-attempt", attemptId, reason, acknowledgeUnknownResult }),
    onCancelTeam: (reason: string) => teamCommand.mutate({ kind: "cancel", reason }),
    onVerifyIntegration: (targetRef: string) => teamCommand.mutate({ kind: "integration", targetRef }),
    onResolveIntegration: (integrationId: string, action: "retry" | "repair" | "resume", reason: string) => teamCommand.mutate({ kind: "resolve-integration", integrationId, action, reason }),
    onCleanupWorktree: (worktreeId: string) => teamCommand.mutate({ kind: "cleanup", worktreeId }),
  };

  return (
    <div className="h-dvh min-h-[520px] overflow-hidden bg-canvas text-ink">
      {settingsOpen && settingsSection ? <SettingsPage section={settingsSection}
        onClose={() => { window.location.hash = ""; }} theme={theme} onThemeChange={setTheme}
        onSaved={() => { setReasoningChoices({}); void queryClient.invalidateQueries({ queryKey: ["runtime-config"] }); }}
        onRestored={(conversation, open) => {
          queryClient.setQueryData<Conversation[]>(conversationsKey, (current = []) => [conversation, ...current.filter(item => item.id !== conversation.id)]);
          queryClient.setQueryData(["conversations", conversation.id], conversation);
          void queryClient.invalidateQueries({ queryKey: conversationsKey, exact: true });
          useSidebarStore.getState().setCollapsed(workspaceKey(conversation.workspace), false);
          if (open) { setSearch(""); setSelectedId(conversation.id); window.location.hash = ""; }
        }}
        onDelete={conversation => deleteConversation.mutateAsync(conversation)}
        mcpPanel={<div className="space-y-5">
          <label className="block text-sm font-medium">项目<select aria-label="MCP 配置项目" value={mcpWorkspace ?? ""} disabled={saveMcpServer.isPending || deleteMcpServer.isPending} onChange={event => { setMcpWorkspaceChoice(event.target.value); setMcpMessage(undefined); saveMcpServer.reset(); deleteMcpServer.reset(); }} className="mt-2 block h-10 w-full rounded-xl border border-line bg-surface px-3 text-xs font-normal">{mcpWorkspaces.map(path => <option key={path} value={path}>{path}</option>)}</select></label>
          {!mcpWorkspace && <p className="text-sm text-ink-muted">请先新建项目，再配置 MCP 插件。</p>}
          <McpConfigModal key={mcpWorkspace} embedded open workspace={mcpWorkspace} config={mcpQuery.data}
            loading={mcpQuery.isLoading} saving={saveMcpServer.isPending}
            deleting={deleteMcpServer.isPending ? deleteMcpServer.variables?.name : undefined}
            error={mcpQuery.error ? errorMessage(mcpQuery.error) : saveMcpServer.error ? errorMessage(saveMcpServer.error) : deleteMcpServer.error ? errorMessage(deleteMcpServer.error) : undefined}
            message={mcpMessage} onSave={server => { setMcpMessage(undefined); saveMcpServer.mutate(server); }}
            onDelete={name => mcpWorkspace && deleteMcpServer.mutate({ workspace: mcpWorkspace, name })}
            onClose={() => { window.location.hash = ""; }} />
        </div>} /> : <>
      <div className="grid h-full min-h-0 grid-cols-1 lg:grid-cols-[264px_minmax(0,1fr)] xl:grid-cols-[264px_minmax(0,1fr)_320px]">
        <div className="hidden min-h-0 lg:block">
          <ConversationSidebar onOpenSettings={openSettings} conversations={filteredConversations} selectedId={selectedId} search={search} loading={conversationsQuery.isLoading} creating={createConversation.isPending} creatingWorkspace={createConversation.variables} onSearch={setSearch} onSelect={setSelectedId} onCreateProject={() => setWorkspacePickerOpen(true)} onCreateConversation={(workspace) => createConversation.mutate(workspace)} onArchive={(conversation) => archiveConversation.mutate(conversation)} onDelete={confirmDeleteConversation} deletingId={deleteConversation.isPending ? deleteConversation.variables.id : undefined} />
        </div>

        <div className="relative flex min-h-0 min-w-0 flex-col">
          <ChatWorkspace planMode={planMode} planLocked={planningActive} onPlanModeChange={selectPlanMode}
            teams={teamsQuery.data} team={team} teamBusy={teamCommand.isPending}
            teamError={teamsQuery.error ? errorMessage(teamsQuery.error) : teamCommand.error ? errorMessage(teamCommand.error) : undefined}
            onResumeAttempt={(attemptId, reason, acknowledgeUnknownResult) => teamCommand.mutate({ kind: "resume-attempt", attemptId, reason, acknowledgeUnknownResult })}
            onResolveIntegration={(integrationId, action, reason) => teamCommand.mutate({ kind: "resolve-integration", integrationId, action, reason })}
            onPauseTeam={() => teamCommand.mutate({ kind: "pause", reason: "用户暂停团队，保留工作现场" })}
            onResumeTeam={() => teamCommand.mutate({ kind: "resume", reason: "用户检查并恢复团队" })}
            planningSnapshot={plansQuery.data} planBusy={planDecision.isPending || exitPlan.isPending}
            planError={planDecision.error ? errorMessage(planDecision.error) : undefined}
            onPlanDecision={(plan, decision) => selectedId && planDecision.mutate({ conversationId: selectedId, plan, decision }, {
              onSuccess: () => { if (decision === "reject") setDraft("请修改方案："); },
            })}
            planPanel={<PlanPreview key={selectedId} snapshot={plansQuery.data} busy={sending || planDecision.isPending || exitPlan.isPending || Boolean(liveRun && isRunActive(liveRun.status))}
              error={planDecision.error ? errorMessage(planDecision.error) : undefined}
              onDecision={(plan, decision) => selectedId && planDecision.mutate({ conversationId: selectedId, plan, decision })}
              onExit={() => selectedId && exitPlan.mutate(selectedId)} onRevise={() => setDraft("请修改方案：")} />}
            onOpenSettings={openModelSettings} key={selectedId} attachments={attachments} onAttachments={setAttachments} teamEnabled={teamEnabled} useTeam={useTeam} onTeamChange={selectTeam} reasoningEffort={reasoningEffort} reasoningOptions={reasoningOptions} reasoningDefault={runtimeQuery.data?.reasoning?.default_level} onReasoningChange={selectReasoning} historyRuns={activityQuery.data} historyLoading={activityQuery.isFetching} historyError={activityQuery.isError} readOnly={readOnly} onReadOnlyChange={selectReadOnly} webSearch={webSearch} webSearchAvailable={webSearchAvailable} onWebSearchChange={selectWebSearch} title={selectedConversation?.title} messages={messagesQuery.data ?? []} loading={Boolean(selectedId && messagesQuery.isLoading)} run={liveRun} draft={draft} sending={sending} cancelling={Boolean(runId && cancellingRuns.includes(runId))} approval={pendingApproval} approvalBusy={Boolean(pendingApproval && decidingRuns.includes(pendingApproval.run_id))} runtimeModel={runtimeQuery.data?.model} teamLeadActive={teamLeadActive} workspace={selectedConversation?.workspace ?? runtimeQuery.data?.workspace} theme={theme} onDraft={setDraft} onSend={send} onCancel={() => runId && cancelRun.mutate(runId)} onApprovalDecision={(decision) => selectedId && pendingApproval && decideApproval.mutate({ conversationId: selectedId, targetRunId: pendingApproval.run_id, approvalId: pendingApproval.id, decision })} onOpenLeft={() => setLeftOpen(true)} onOpenRight={() => setRightOpen(true)} onOpenMcp={() => { setMcpWorkspaceChoice(selectedConversation?.workspace ?? runtimeQuery.data?.workspace); setMcpMessage(undefined); window.location.hash = "settings/mcp"; }} onToggleTheme={cycleTheme} />
        </div>

        <div className="hidden min-h-0 xl:block"><InspectorPanel run={liveRun} runtime={activeRuntime} tasks={tasksQuery.data} tasksLoading={tasksQuery.isLoading} taskBusy={createTask.isPending || Boolean(liveRun && isRunActive(liveRun.status))} taskList={taskListQuery.data} onContinueTask={continueTask} onCreateTask={(input) => createTask.mutate(input)} {...teamPanelProps} /></div>
      </div>

      <Drawer open={leftOpen} side="left" onClose={() => setLeftOpen(false)}>
        <ConversationSidebar onOpenSettings={openSettings} mobile conversations={filteredConversations} selectedId={selectedId} search={search} loading={conversationsQuery.isLoading} creating={createConversation.isPending} creatingWorkspace={createConversation.variables} onSearch={setSearch} onSelect={(id) => { setSelectedId(id); setLeftOpen(false); }} onCreateProject={() => { setLeftOpen(false); setWorkspacePickerOpen(true); }} onCreateConversation={(workspace) => createConversation.mutate(workspace)} onArchive={(conversation) => archiveConversation.mutate(conversation)} onDelete={confirmDeleteConversation} deletingId={deleteConversation.isPending ? deleteConversation.variables.id : undefined} onClose={() => setLeftOpen(false)} />
      </Drawer>
      <Drawer open={rightOpen} side="right" onClose={() => setRightOpen(false)} width="min(90vw, 360px)"><InspectorPanel mobile run={liveRun} runtime={activeRuntime} tasks={tasksQuery.data} tasksLoading={tasksQuery.isLoading} taskBusy={createTask.isPending || Boolean(liveRun && isRunActive(liveRun.status))} taskList={taskListQuery.data} onContinueTask={continueTask} onCreateTask={(input) => createTask.mutate(input)} onClose={() => setRightOpen(false)} {...teamPanelProps} /></Drawer>

      <WorkspacePicker
        open={workspacePickerOpen}
        initialPath={runtimeQuery.data?.workspace}
        loading={workspacesQuery.isFetching || createConversation.isPending}
        listing={workspacesQuery.data}
        error={workspacesQuery.error ? errorMessage(workspacesQuery.error) : undefined}
        onBrowse={(path) => { setWorkspacePath(path); setWorkspaceSearch(""); }}
        onSearch={setWorkspaceSearch}
        onConfirm={(path) => createConversation.mutate(path)}
        onClose={() => { if (!createConversation.isPending) { setWorkspacePickerOpen(false); setWorkspacePath(undefined); setWorkspaceSearch(""); } }}
      />

      </>}
      {notice && <div role="alert" className="fixed bottom-4 left-1/2 z-[70] flex max-w-[calc(100vw-2rem)] -translate-x-1/2 items-center gap-2 rounded-xl border border-danger/20 bg-surface px-3 py-2.5 text-xs text-danger shadow-panel"><AlertCircle className="size-4 shrink-0" /><span className="min-w-0">{notice}</span><button type="button" aria-label="关闭提示" onClick={() => setNotice(undefined)}><X className="size-3.5" /></button></div>}
    </div>
  );
}

function Drawer({ open, side, width = "min(88vw, 300px)", onClose, children }: { open: boolean; side: "left" | "right"; width?: string; onClose: () => void; children: React.ReactNode }) {
  return <div className={cx("fixed inset-0 z-50 transition lg:hidden", open ? "pointer-events-auto" : "pointer-events-none")} aria-hidden={!open}><button type="button" aria-label="关闭面板" onClick={onClose} className={cx("absolute inset-0 bg-black/45 backdrop-blur-[1px] transition-opacity", open ? "opacity-100" : "opacity-0")} /><div style={{ width }} className={cx("absolute inset-y-0 shadow-2xl transition-transform duration-200 motion-reduce:transition-none", side === "left" ? "left-0" : "right-0", open ? "translate-x-0" : side === "left" ? "-translate-x-full" : "translate-x-full")}>{children}</div></div>;
}

function useTheme() {
  const [theme, setTheme] = useState<ThemeMode>(() => {
    const stored = localStorage.getItem("codeagent.theme");
    return stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
  });
  useEffect(() => {
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => document.documentElement.classList.toggle("dark", theme === "dark" || theme === "system" && query.matches);
    apply();
    query.addEventListener("change", apply);
    localStorage.setItem("codeagent.theme", theme);
    return () => query.removeEventListener("change", apply);
  }, [theme]);
  const cycleTheme = () => setTheme((value) => value === "system" ? "light" : value === "light" ? "dark" : "system");
  return { theme, cycleTheme, setTheme };
}

function showError(error: unknown, setNotice: (message: string) => void) {
  setNotice(errorMessage(error));
}

function errorMessage(error: unknown) {
  return error instanceof ApiError ? error.message : error instanceof Error ? error.message : "操作失败，请稍后重试";
}
