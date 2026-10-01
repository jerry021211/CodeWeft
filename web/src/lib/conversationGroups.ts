import type { Conversation } from "@/types/api";

export function workspaceKey(path: string) {
  const normalized = path.replace(/\\/g, "/").replace(/\/+$/, "") || (path ? "/" : "");
  return /^[a-z]:[\\/]|^\\\\/i.test(path) ? normalized.toLowerCase() : normalized;
}

export function groupConversations(conversations: Conversation[]) {
  const groups = new Map<string, { key: string; name: string; workspace: string; conversations: Conversation[] }>();
  const timestamp = (value: string) => Date.parse(value) || 0;
  for (const conversation of [...conversations].sort((a, b) => timestamp(b.updated_at) - timestamp(a.updated_at))) {
    const key = workspaceKey(conversation.workspace);
    let group = groups.get(key);
    if (!group) {
      group = {
        key,
        name: conversation.workspace.split(/[\\/]+/).filter(Boolean).at(-1) || conversation.workspace || "未绑定工作区",
        workspace: conversation.workspace,
        conversations: [],
      };
      groups.set(key, group);
    }
    group.conversations.push(conversation);
  }
  return [...groups.values()];
}
