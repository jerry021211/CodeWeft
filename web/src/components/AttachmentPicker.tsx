import { forwardRef, useImperativeHandle, useRef, useState } from "react";
import { Paperclip, X } from "lucide-react";
import type { Attachment } from "@/types/api";

export type AttachmentPickerHandle = { open: () => void };
export const AttachmentPicker = forwardRef<AttachmentPickerHandle, {
  value: Attachment[]; onChange: (value: Attachment[]) => void; disabled?: boolean; onBusyChange: (busy: boolean) => void; hideTrigger?: boolean;
}>(function AttachmentPicker({ value, onChange, disabled, onBusyChange, hideTrigger }, ref) {
  const input = useRef<HTMLInputElement>(null);
  const [error, setError] = useState("");
  const [reading, setReading] = useState(false);
  useImperativeHandle(ref, () => ({ open: () => { if (!disabled && !reading) input.current?.click(); } }), [disabled, reading]);
  const add = async (files: File[]) => {
    setError("");
    if (value.length + files.length > 8 || files.some(file => !file.size || file.size > 10 * 1024 * 1024)
      || value.reduce((size, item) => size + item.data.length * 3 / 4, 0) + files.reduce((size, file) => size + file.size, 0) > 20 * 1024 * 1024) {
      setError("最多 8 个附件，单个不超过 10 MiB，总计不超过 20 MiB，且不能为空。");
      return;
    }
    setReading(true);
    onBusyChange(true);
    try {
      const additions = await Promise.all(files.map(file => new Promise<Attachment>((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error("读取附件失败"));
        reader.onload = () => {
          const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
          const type = ({ csv: "text/csv", md: "text/markdown", json: "application/json", wav: "audio/wav", mp3: "audio/mpeg", pdf: "application/pdf" } as Record<string, string>)[extension] || file.type || "text/plain";
          resolve({ name: file.name, media_type: type === "audio/x-wav" ? "audio/wav" : type,
            data: String(reader.result).split(",", 2)[1] ?? "" });
        };
        reader.readAsDataURL(file);
      })));
      onChange([...value, ...additions]);
    } catch {
      setError("读取附件失败，请重新选择。");
    } finally {
      setReading(false);
      onBusyChange(false);
    }
  };
  return <div className={hideTrigger && !value.length && !error ? "contents" : "px-3 pt-3 pb-1"}>
    <input ref={input} type="file" multiple className="hidden" accept="image/png,image/jpeg,image/webp,image/gif,application/pdf,text/*,.json,.csv,.md,.wav,.mp3"
      onChange={event => { const files = Array.from(event.target.files ?? []); event.target.value = ""; void add(files); }} />
    <div className="flex flex-wrap items-center gap-2">
      {!hideTrigger && <button type="button" disabled={disabled || reading} onClick={() => input.current?.click()}
        className="inline-flex items-center gap-1 text-xs text-ink-muted disabled:opacity-40" title="图片、PDF、文本、WAV 或 MP3；音频可使用独立语音识别模型">
        <Paperclip className="size-3.5" />{reading ? "读取中…" : "添加附件"}
      </button>}
      {value.map((item, index) => <span key={index} className="inline-flex max-w-48 items-center gap-1.5 rounded-lg border border-line bg-surface-muted px-2 py-1.5 text-xs text-ink-muted">
        <Paperclip className="size-3 shrink-0" />
        <span className="truncate" title={item.name}>{item.name}</span>
        <button type="button" disabled={disabled || reading} aria-label={`移除 ${item.name}`} onClick={() => onChange(value.filter((_, i) => i !== index))}><X className="size-3" /></button>
      </span>)}
    </div>
    {error && <p role="alert" className="mt-1 text-xs text-danger">{error}</p>}
  </div>;
});
