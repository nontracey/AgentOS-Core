import { ArrowUp, Brain, FileText, Image as ImageIcon, Paperclip, Square, X } from "lucide-react";
import { useEffect, useRef, useState, type ClipboardEvent, type DragEvent, type KeyboardEvent } from "react";
import { toast } from "sonner";
import { upload } from "@/lib/api";
import { bytes, cn } from "@/lib/format";
import type { Attachment } from "@/lib/types";
import { Spinner, Tooltip } from "@/components/ui";

export interface ComposerHandle {
  text: string;
  attachments: Attachment[];
}

export function Composer({
  onSend,
  onStop,
  busy,
  think,
  onThinkChange,
  supportsThinking,
  supportsVision,
  placeholder,
  autoFocus,
  initialText = "",
}: {
  onSend: (text: string, attachments: Attachment[]) => void;
  onStop: () => void;
  busy: boolean;
  think: "auto" | "on" | "off";
  onThinkChange: (value: "auto" | "on" | "off") => void;
  supportsThinking: boolean;
  supportsVision: boolean;
  placeholder?: string;
  autoFocus?: boolean;
  initialText?: string;
}) {
  const [text, setText] = useState(initialText);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(0);
  const [dragging, setDragging] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 260)}px`;
  }, [text]);

  useEffect(() => {
    if (autoFocus) area.current?.focus();
  }, [autoFocus]);

  async function addFiles(files: FileList | File[]) {
    for (const file of Array.from(files)) {
      setUploading((n) => n + 1);
      try {
        const item = await upload<Attachment>("/v1/chat/attachments", file, file.name);
        setAttachments((list) => [...list, item]);
        if (item.kind === "image" && !supportsVision) toast.message("当前模型不支持看图", { description: "换一个带视觉能力的模型，图片才会发送给它。" });
      } catch (error) {
        toast.error((error as Error).message);
      } finally {
        setUploading((n) => n - 1);
      }
    }
  }

  function submit() {
    const value = text.trim();
    if (busy || uploading || (!value && attachments.length === 0)) return;
    onSend(value, attachments);
    setText("");
    setAttachments([]);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  function onPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(event.clipboardData.files || []);
    if (files.length) {
      event.preventDefault();
      void addFiles(files);
    }
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    if (event.dataTransfer.files.length) void addFiles(event.dataTransfer.files);
  }

  const thinkingOn = think === "on" || (think === "auto" && supportsThinking);

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
      className={cn(
        "rounded-[18px] border border-line bg-surface shadow-[var(--shadow-card)] transition-[border-color,box-shadow] focus-within:border-line-strong",
        dragging && "border-accent live-glow",
      )}
    >
      {(attachments.length > 0 || uploading > 0) && (
        <div className="flex flex-wrap gap-2 px-3 pt-3">
          {attachments.map((item) => (
            <span key={item.asset_id} className="inline-flex max-w-60 items-center gap-2 rounded-[9px] bg-surface-2 py-1.5 pr-1.5 pl-2.5 text-[12.5px]">
              {item.kind === "image" ? <ImageIcon className="size-3.5 text-ink-3" /> : <FileText className="size-3.5 text-ink-3" />}
              <span className="truncate">{item.name}</span>
              <span className="text-ink-3">{bytes(item.size)}</span>
              <button aria-label="移除附件" onClick={() => setAttachments((list) => list.filter((a) => a.asset_id !== item.asset_id))} className="rounded p-0.5 text-ink-3 hover:bg-surface-3 hover:text-ink">
                <X className="size-3.5" />
              </button>
            </span>
          ))}
          {uploading > 0 && (
            <span className="inline-flex items-center gap-2 rounded-[9px] bg-surface-2 px-2.5 py-1.5 text-[12.5px] text-ink-3">
              <Spinner className="size-3.5" />
              上传中
            </span>
          )}
        </div>
      )}
      <textarea
        ref={area}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        onPaste={onPaste}
        rows={1}
        placeholder={placeholder || "输入问题，Enter 发送，Shift + Enter 换行"}
        className="block max-h-[260px] min-h-[52px] w-full resize-none bg-transparent px-4 pt-3.5 pb-1 text-[15px] leading-relaxed outline-none placeholder:text-ink-3"
      />
      <div className="flex items-center gap-1 px-2.5 pb-2.5">
        <input ref={fileInput} type="file" multiple hidden onChange={(e) => e.target.files && void addFiles(e.target.files)} />
        <Tooltip content="添加图片或文本文件">
          <button onClick={() => fileInput.current?.click()} className="rounded-[9px] p-2 text-ink-3 hover:bg-surface-3 hover:text-ink" aria-label="添加附件">
            <Paperclip className="size-[18px]" />
          </button>
        </Tooltip>
        <Tooltip content={supportsThinking ? "深度思考：先推理再回答，更准但更慢" : "当前模型没有单独的思考模式"}>
          <button
            onClick={() => onThinkChange(thinkingOn ? "off" : "on")}
            disabled={!supportsThinking && think !== "on"}
            className={cn(
              "inline-flex h-8 items-center gap-1.5 rounded-[9px] px-2.5 text-[13px] font-medium transition-colors disabled:opacity-40",
              thinkingOn ? "bg-accent-soft text-accent-ink" : "text-ink-3 hover:bg-surface-3 hover:text-ink",
            )}
          >
            <Brain className="size-4" />
            深度思考
          </button>
        </Tooltip>
        <div className="ml-auto">
          {busy ? (
            <button onClick={onStop} aria-label="停止生成" className="flex size-9 items-center justify-center rounded-full bg-ink text-surface hover:opacity-85">
              <Square className="size-3.5 fill-current" />
            </button>
          ) : (
            <button
              onClick={submit}
              aria-label="发送"
              disabled={uploading > 0 || (!text.trim() && attachments.length === 0)}
              className="flex size-9 items-center justify-center rounded-full bg-primary text-on-primary transition-opacity hover:opacity-90 disabled:opacity-30"
            >
              <ArrowUp className="size-[18px]" strokeWidth={2.4} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
