import { AlertTriangle, Check, ChevronRight, Copy, FileText, Image as ImageIcon, Pencil, RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";
import { Markdown } from "@/components/Markdown";
import { Button, IconButton, Spinner } from "@/components/ui";
import { cn, duration, modelLabel } from "@/lib/format";
import type { ChatMessage, GpuSnapshot } from "@/lib/types";

export function ThinkingBlock({ text, live, seconds }: { text: string; live: boolean; seconds?: number }) {
  const [open, setOpen] = useState(live);
  useEffect(() => {
    if (!live) setOpen(false);
  }, [live]);
  if (!text) return null;
  return (
    <div className="mb-3">
      <button onClick={() => setOpen(!open)} className="inline-flex items-center gap-1.5 text-[13px] text-ink-3 hover:text-ink-2">
        <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
        {live ? (
          <span className="animate-pulse">正在思考…</span>
        ) : (
          <span>{seconds === undefined ? "思考过程" : seconds < 1 ? "思考了片刻" : `思考了 ${duration(seconds)}`}</span>
        )}
      </button>
      {open && (
        <div className="mt-2 max-h-80 overflow-y-auto border-l-2 border-line pl-4 text-[13.5px] leading-relaxed whitespace-pre-wrap text-ink-3">
          {text}
        </div>
      )}
    </div>
  );
}

function Stats({ message }: { message: ChatMessage }) {
  const s = message.stats || {};
  const parts: string[] = [];
  if (s.tokens_per_second) parts.push(`${s.tokens_per_second} tok/s`);
  if (s.output_tokens) parts.push(`${s.output_tokens} tokens`);
  if (s.first_token_seconds !== undefined) parts.push(`首字 ${s.first_token_seconds.toFixed(1)} 秒`);
  if (s.load_seconds && s.load_seconds > 1) parts.push(`加载模型 ${s.load_seconds.toFixed(0)} 秒`);
  if (!parts.length) return null;
  return (
    <span className="tabular flex flex-wrap gap-x-3 text-[12px] text-ink-3">
      {parts.map((p) => (
        <span key={p}>{p}</span>
      ))}
    </span>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <IconButton
      label={copied ? "已复制" : "复制"}
      size="sm"
      onClick={() => {
        void navigator.clipboard?.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 1400);
      }}
    >
      {copied ? <Check className="size-3.5 text-green" /> : <Copy className="size-3.5" />}
    </IconButton>
  );
}

export function UserMessage({
  message,
  onEdit,
  editable,
}: {
  message: Pick<ChatMessage, "id" | "content" | "attachments">;
  onEdit?: (text: string) => void;
  editable?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.content);
  if (editing) {
    return (
      <div className="ml-auto w-full max-w-[85%] rounded-[16px] border border-line-strong bg-surface p-3">
        <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={Math.min(10, draft.split("\n").length + 1)} className="w-full resize-none bg-transparent text-[15px] leading-relaxed outline-none" autoFocus />
        <div className="mt-2 flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
            取消
          </Button>
          <Button
            size="sm"
            variant="primary"
            disabled={!draft.trim()}
            onClick={() => {
              setEditing(false);
              onEdit?.(draft.trim());
            }}
          >
            重新发送
          </Button>
        </div>
      </div>
    );
  }
  return (
    <div className="group flex flex-col items-end gap-1.5">
      {message.attachments?.length > 0 && (
        <div className="flex flex-wrap justify-end gap-1.5">
          {message.attachments.map((a) => (
            <span key={a.asset_id} className="inline-flex items-center gap-1.5 rounded-[9px] border border-line px-2 py-1 text-[12px] text-ink-2">
              {a.kind === "image" ? <ImageIcon className="size-3.5" /> : <FileText className="size-3.5" />}
              {a.name}
            </span>
          ))}
        </div>
      )}
      {message.content && (
        <div className="max-w-[85%] rounded-[16px] rounded-br-[6px] bg-surface-3 px-4 py-2.5 text-[15px] leading-relaxed whitespace-pre-wrap">
          {message.content}
        </div>
      )}
      <div className="flex h-7 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
        <CopyButton text={message.content} />
        {editable && onEdit && (
          <IconButton label="修改后重新发送" size="sm" onClick={() => setEditing(true)}>
            <Pencil className="size-3.5" />
          </IconButton>
        )}
      </div>
    </div>
  );
}

const STATUS_TEXT: Record<string, string> = {
  connecting: "正在连接模型…",
  loading: "正在把模型加载到显存…",
  thinking: "思考中…",
  generating: "",
};

export function AssistantMessage({
  message,
  live,
  onRegenerate,
  isLast,
}: {
  message: ChatMessage;
  live?: { status: string; thinking: string; content: string; gpu?: GpuSnapshot; thinkingSeconds?: number; notes: string[] };
  onRegenerate?: () => void;
  isLast?: boolean;
}) {
  const content = live ? live.content : message.content;
  const thinking = live ? live.thinking : message.thinking;
  const status = live?.status || "";
  const waiting = status === "waiting_gpu";
  const thinkingSeconds = live?.thinkingSeconds ?? (thinking ? message.stats?.thinking_seconds : undefined);
  return (
    <div className="group animate-rise">
      <div className="mb-2 flex items-center gap-2 text-[12.5px] text-ink-3">
        <span className="font-medium text-ink-2">{modelLabel(message.model) || "助手"}</span>
      </div>
      {waiting && live?.gpu?.holder && (
        <div className="mb-3 flex items-center gap-3 rounded-[12px] bg-accent-soft px-3.5 py-2.5 text-[13px] text-accent-ink">
          <Spinner className="text-accent-ink" />
          <span>
            显卡正在处理「{live.gpu.holder.label}」，完成后马上开始回答
            {live.gpu.waiting.length > 1 && `（前面还有 ${live.gpu.waiting.length - 1} 项）`}
          </span>
        </div>
      )}
      {live?.notes?.map((note) => (
        <div key={note} className="mb-2 text-[12.5px] text-ink-3">
          {note}
        </div>
      ))}
      <ThinkingBlock text={thinking} live={status === "thinking"} seconds={thinkingSeconds} />
      {content ? (
        <div className={cn(live && status === "generating" && "caret-host")}>
          <Markdown source={content} />
          {live && status === "generating" && <span className="caret" />}
        </div>
      ) : live && !waiting ? (
        <div className="flex items-center gap-2 text-[13.5px] text-ink-3">
          <Spinner />
          {STATUS_TEXT[status] || "生成中…"}
        </div>
      ) : null}
      {!live && message.status === "error" && (
        <div className="mt-2 flex items-start gap-2 rounded-[12px] bg-red-soft px-3.5 py-2.5 text-[13px] leading-relaxed text-red">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          {message.error || "生成失败"}
        </div>
      )}
      {!live && message.status === "stopped" && <div className="mt-2 text-[12.5px] text-ink-3">已停止</div>}
      {!live && message.status === "interrupted" && <div className="mt-2 text-[12.5px] text-ink-3">服务重启时中断，可以重新生成</div>}
      {!live && (
        <div className="mt-2 flex items-center gap-3">
          <div className="flex items-center gap-0.5">
            {message.content && <CopyButton text={message.content} />}
            {isLast && onRegenerate && (
              <IconButton label="重新生成" size="sm" onClick={onRegenerate}>
                <RotateCcw className="size-3.5" />
              </IconButton>
            )}
          </div>
          <Stats message={message} />
        </div>
      )}
    </div>
  );
}
