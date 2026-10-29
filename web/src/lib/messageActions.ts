export function quoteMessage(draft: string, content: string): string {
  const quote = content.replace(/\r\n?/g, "\n").split("\n").map(line => `> ${line}`).join("\n");
  return `${draft}${draft ? "\n\n" : ""}${quote}\n\n`;
}

export async function copyMessage(content: string): Promise<void> {
  if (!navigator.clipboard?.writeText) throw new Error("当前浏览器不支持复制，请选中文字手动复制。");
  try {
    await navigator.clipboard.writeText(content);
  } catch {
    throw new Error("复制失败，请允许剪贴板权限或选中文字手动复制。");
  }
}
