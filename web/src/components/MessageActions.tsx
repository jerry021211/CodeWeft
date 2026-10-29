import { useEffect, useRef, useState } from "react";
import { Check, Copy, GitBranch, Quote, RotateCcw } from "lucide-react";
import { IconButton } from "@/components/ui";
import { copyMessage } from "@/lib/messageActions";

export function MessageActions({ content, onQuote, onFork, busy }: {
  content: string; onQuote?: () => void; onFork?: (regenerate: boolean) => void; busy?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  const timer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = async () => {
    setError("");
    try {
      await copyMessage(content);
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 2000);
    } catch (error) {
      setCopied(false);
      setError(error instanceof Error ? error.message : "复制失败，请手动复制。");
    }
  };
  return <div className="mt-1">
    <div className="flex items-center gap-0.5" role="group" aria-label="回复操作">
      {onQuote && <IconButton className="size-8" label="引用到输入框" onClick={onQuote}><Quote className="size-4" /></IconButton>}
      <IconButton className="size-8" label={copied ? "已复制" : "复制回复"} onClick={() => void copy()}>{copied ? <Check className="size-4 text-success" /> : <Copy className="size-4" />}</IconButton>
      {onFork && <>
        <IconButton className="size-8" label="在新分支重新生成（共用项目文件）" disabled={busy} onClick={() => onFork(true)}><RotateCcw className="size-4" /></IconButton>
        <IconButton className="size-8" label="从此处创建分支（共用项目文件）" disabled={busy} onClick={() => onFork(false)}><GitBranch className="size-4" /></IconButton>
      </>}
    </div>
    <span className="sr-only" role="status">{copied ? "回复已复制" : ""}</span>
    {error && <p className="mt-1 text-xs text-danger" role="alert">{error}</p>}
  </div>;
}
