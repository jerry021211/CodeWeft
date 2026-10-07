import type { Approval } from "@/types/api";

// Presentation hints never grant permission or assert that a command is safe.
export function approvalPresentation(approval: Approval) {
  const input = approval.input && typeof approval.input === "object" && !Array.isArray(approval.input)
    ? approval.input as Record<string, unknown> : {};
  const value = (key: string) => typeof input[key] === "string" ? (input[key] as string).trim() : "";
  const command = value("command");
  const path = value("file_path") || value("path");
  const purpose = value("description");
  const effects: string[] = [];
  let title = "执行工具操作";
  let scope = "具体访问范围以工具参数为准；当前没有足够信息确认全部影响。";
  if (approval.tool_name === "bash") {
    title = "运行命令";
    scope = "请核对完整命令中的路径。变量、脚本和子进程的实际访问范围可能超出显式路径。";
    // Only flag syntax to inspect, not inferred intent or exhaustive analysis.
    if (/(?:^|[\s;&|])(rm|rmdir|del|remove-item)(?=\s|$)/i.test(command)) effects.push("命令包含删除相关指令，请确认目标文件或目录。");
    if (/\bgit\s+push\b|\b(?:npm|pnpm|yarn)\s+publish\b/i.test(command)) effects.push("命令包含推送或发布指令，请确认目标仓库或服务。");
    if (/\b(?:install|add|pip|choco|winget)\b/i.test(command)) effects.push("命令包含依赖或软件安装相关内容，可能下载文件并改变本地环境。");
    if (!effects.length) effects.push("将在当前执行环境运行命令；调用脚本时也会执行脚本内的操作。");
  } else if (["write_file", "edit_file"].includes(approval.tool_name)) {
    title = approval.tool_name === "write_file" ? "写入文件" : "修改文件";
    effects.push(approval.tool_name === "write_file" ? "写入指定文件；文件已存在时可能覆盖原内容。" : "修改指定文件中的内容。");
    scope = path || "工具未提供明确文件路径，请展开参数核对。";
  } else {
    effects.push("执行下方参数指定的工具操作；工具的外部影响需要结合详情核对。");
    if (path) scope = path;
  }
  return { title, purpose, effects, scope, command,
    explanation: "当前权限规则要求这次操作先获得你的确认。授权只用于本次请求，不代表之后的同类操作也被允许。",
    declined: "本次请求不会获准执行。Agent 会收到拒绝结果，再决定调整操作或停止；此前已完成的修改不会自动撤销。" };
}

export function approvalStatus(approval: Approval, now = Date.now()): Approval["status"] {
  return approval.status === "pending" && approval.expires_at && Date.parse(approval.expires_at) <= now ? "expired" : approval.status;
}
