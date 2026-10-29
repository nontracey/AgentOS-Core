import { Check, Pause, Play, Star, Trash2, Upload } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { cn } from "@/lib/format";
import type { Job, Take } from "@/lib/types";
import { Spinner, Tooltip } from "@/components/ui";

/* One audio element for the whole studio so only one line plays at a time. */
let shared: HTMLAudioElement | null = null;
const audioListeners = new Set<() => void>();
let playingUrl = "";

function player(): HTMLAudioElement {
  if (!shared) {
    shared = new Audio();
    const notify = () => audioListeners.forEach((listener) => listener());
    shared.addEventListener("ended", () => {
      playingUrl = "";
      notify();
    });
    shared.addEventListener("pause", notify);
    shared.addEventListener("play", notify);
  }
  return shared;
}

export function AudioButton({ url, label = "播放", className, size = "md" }: { url?: string; label?: string; className?: string; size?: "sm" | "md" }) {
  const [, force] = useState(0);
  useEffect(() => {
    const listener = () => force((n) => n + 1);
    audioListeners.add(listener);
    return () => {
      audioListeners.delete(listener);
    };
  }, []);
  const active = Boolean(url) && playingUrl === url && shared && !shared.paused;
  return (
    <Tooltip content={active ? "暂停" : label}>
      <button
        type="button"
        disabled={!url}
        onClick={(event) => {
          event.stopPropagation();
          const audio = player();
          if (active) {
            audio.pause();
            return;
          }
          playingUrl = url!;
          audio.src = url!;
          void audio.play();
        }}
        className={cn(
          "inline-flex shrink-0 items-center justify-center rounded-full transition-colors disabled:opacity-30",
          size === "sm" ? "size-6" : "size-8",
          active ? "bg-accent text-on-accent" : "bg-surface-3 text-ink-2 hover:bg-line hover:text-ink",
          className,
        )}
        aria-label={active ? "暂停" : label}
      >
        {active ? <Pause className={size === "sm" ? "size-3" : "size-3.5"} /> : <Play className={cn(size === "sm" ? "size-3" : "size-3.5", "translate-x-px")} />}
      </button>
    </Tooltip>
  );
}

export function FrameBox({
  src,
  aspect,
  children,
  className,
  guides = true,
  live,
  onClick,
}: {
  src?: string;
  aspect: string;
  children?: ReactNode;
  className?: string;
  guides?: boolean;
  live?: boolean;
  onClick?: () => void;
}) {
  const [loaded, setLoaded] = useState(false);
  useEffect(() => setLoaded(false), [src]);
  return (
    <div
      onClick={onClick}
      style={{ aspectRatio: aspect }}
      className={cn(
        "relative overflow-hidden rounded-[6px] border border-line bg-surface-2",
        guides && !src && "frame-guides",
        live && "live-glow",
        onClick && "cursor-pointer",
        className,
      )}
    >
      {src && (
        <img
          src={src}
          alt=""
          loading="lazy"
          decoding="async"
          onLoad={() => setLoaded(true)}
          className={cn("absolute inset-0 size-full object-cover transition-opacity duration-300", loaded ? "opacity-100" : "opacity-0")}
        />
      )}
      {src && !loaded && <div className="skeleton absolute inset-0" />}
      {children}
    </div>
  );
}

export function VideoTake({ take, aspect, autoPlay }: { take: Take; aspect: string; autoPlay?: boolean }) {
  const ref = useRef<HTMLVideoElement>(null);
  return (
    <video
      ref={ref}
      src={take.preview_url || take.url}
      poster={take.poster_url}
      style={{ aspectRatio: aspect }}
      className="w-full rounded-[6px] border border-line bg-black object-cover"
      controls
      muted
      loop
      playsInline
      autoPlay={autoPlay}
      preload="metadata"
    />
  );
}

export function JobBadge({ jobs }: { jobs: Job[] }) {
  const running = jobs.find((j) => j.state === "running");
  const queued = jobs.filter((j) => j.state === "queued").length;
  const failed = jobs.find((j) => j.state === "failed");
  if (running) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-accent-soft px-2 py-0.5 text-[11.5px] font-medium text-accent-ink">
        <Spinner className="size-3 text-accent-ink" />
        生成中{queued ? ` +${queued}` : ""}
      </span>
    );
  }
  if (queued) return <span className="rounded-full bg-surface-3 px-2 py-0.5 text-[11.5px] text-ink-2">排队 {queued}</span>;
  if (jobs.some((j) => j.state === "approval")) return <span className="rounded-full bg-accent-soft px-2 py-0.5 text-[11.5px] text-accent-ink">待批准</span>;
  const blocked = jobs.find((j) => j.state === "blocked");
  if (blocked) {
    return (
      <Tooltip content={blocked.error}>
        <span className="rounded-full bg-red-soft px-2 py-0.5 text-[11.5px] text-red">待核对</span>
      </Tooltip>
    );
  }
  if (failed) {
    return (
      <Tooltip content={failed.error || "失败"}>
        <span className="rounded-full bg-red-soft px-2 py-0.5 text-[11.5px] text-red">失败</span>
      </Tooltip>
    );
  }
  return null;
}

/** Candidate strip: every take of a slot; the selected one carries the gold ring. */
export function TakeStrip({
  takes,
  selectedId,
  onSelect,
  onDelete,
  onRate,
  aspect,
  size = 64,
}: {
  takes: Take[];
  selectedId?: string;
  onSelect: (take: Take) => void;
  onDelete?: (take: Take) => void;
  onRate?: (take: Take, rating: number) => void;
  aspect: string;
  size?: number;
}) {
  if (!takes.length) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {takes.map((take, index) => {
        const chosen = take.id === selectedId;
        return (
          <div key={take.id} className="group relative" style={{ width: size }}>
            <button
              type="button"
              onClick={() => onSelect(take)}
              style={{ aspectRatio: aspect }}
              className={cn(
                "relative block w-full overflow-hidden rounded-[6px] border bg-surface-2 transition-shadow",
                chosen ? "border-accent shadow-[0_0_0_2px_var(--accent)]" : "border-line hover:border-line-strong",
              )}
              aria-label={`选择第 ${index + 1} 个结果`}
            >
              {take.thumb && <img src={take.thumb} alt="" loading="lazy" className="absolute inset-0 size-full object-cover" />}
              {chosen && (
                <span className="absolute top-1 right-1 flex size-4 items-center justify-center rounded-full bg-accent text-on-accent">
                  <Check className="size-3" strokeWidth={3} />
                </span>
              )}
              {take.rating > 0 && <Star className="absolute bottom-1 left-1 size-3.5 fill-accent text-accent" />}
            </button>
            <div className="pointer-events-none absolute -top-2 -right-2 flex gap-1 opacity-0 transition-opacity group-hover:pointer-events-auto group-hover:opacity-100">
              {onRate && (
                <button onClick={() => onRate(take, take.rating > 0 ? 0 : 1)} aria-label="标记喜欢" className="rounded-full border border-line bg-surface p-1 text-ink-3 shadow hover:text-accent-ink">
                  <Star className={cn("size-3", take.rating > 0 && "fill-accent text-accent")} />
                </button>
              )}
              {onDelete && (
                <button onClick={() => onDelete(take)} aria-label="删除这个结果" className="rounded-full border border-line bg-surface p-1 text-ink-3 shadow hover:text-red">
                  <Trash2 className="size-3" />
                </button>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function UploadButton({
  accept,
  onFile,
  children,
  className,
}: {
  accept: string;
  onFile: (file: File) => void;
  children?: ReactNode;
  className?: string;
}) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <input
        ref={input}
        type="file"
        accept={accept}
        hidden
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) onFile(file);
          e.target.value = "";
        }}
      />
      <button
        type="button"
        onClick={() => input.current?.click()}
        className={cn("inline-flex h-8 items-center gap-1.5 rounded-[8px] px-2.5 text-[13px] text-ink-2 hover:bg-surface-3 hover:text-ink", className)}
      >
        <Upload className="size-3.5" />
        {children || "上传"}
      </button>
    </>
  );
}
