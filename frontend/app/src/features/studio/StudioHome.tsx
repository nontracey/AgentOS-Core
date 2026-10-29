import { Clapperboard, Plus, RectangleHorizontal, RectangleVertical, Square, TriangleAlert } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { cn, duration, relativeTime } from "@/lib/format";
import type { Project, ProjectSummary, ProjectView, StudioOverview, StudioProgress } from "@/lib/types";
import { Badge, Button, Dialog, Empty, Field, Segmented, Spinner } from "@/components/ui";
import { FORMAT_ASPECT, GENRES } from "./labels";
import { useOverview, useProjects } from "./data";

function stages(progress: StudioProgress) {
  return [
    { label: "分镜", done: progress.shots > 0 ? 1 : 0, total: 1 },
    { label: "定妆", done: progress.refs, total: progress.characters },
    { label: "画面", done: progress.frames, total: progress.shots },
    { label: "配音", done: progress.voices, total: progress.lines },
    { label: "动态", done: progress.clips, total: progress.moving },
    { label: "成片", done: progress.rendered ? 1 : 0, total: 1 },
  ];
}

export function StageMeter({ progress, className }: { progress: StudioProgress; className?: string }) {
  return (
    <div className={cn("flex gap-1", className)}>
      {stages(progress).map((stage) => {
        const ratio = stage.total ? stage.done / stage.total : 0;
        return (
          <div key={stage.label} className="min-w-0 flex-1">
            <div className="h-1 overflow-hidden rounded-full bg-line">
              <div className="h-full rounded-full bg-accent" style={{ width: `${Math.min(1, ratio) * 100}%` }} />
            </div>
            <div className="mt-1 truncate text-[11px] text-ink-3">{stage.label}</div>
          </div>
        );
      })}
    </div>
  );
}

const HOURS = [
  { hours: 8, label: "8 小时" },
  { hours: 24, label: "24 小时" },
  { hours: 168, label: "7 天" },
];

function Notice({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-start gap-x-3 gap-y-2 rounded-[12px] border border-line bg-surface-2 px-4 py-3 text-[13px] leading-relaxed text-ink-2">
      <TriangleAlert className="mt-0.5 size-4 shrink-0 text-accent-ink" />
      <span className="min-w-0 flex-1">{children}</span>
      {action}
    </div>
  );
}

export function GrantButtons() {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(0);
  async function grant(hours: number) {
    setBusy(hours);
    try {
      await api("/v1/studio/permissions", { body: { hours } });
      toast.success("已允许工作室直接使用生成节点", { description: "到期自动失效，可在设备页撤销；已经在等待批准的项目还需要点一次批准" });
      void qc.invalidateQueries({ queryKey: ["studio", "overview"] });
    } catch (error) {
      toast.error((error as Error).message);
    } finally {
      setBusy(0);
    }
  }
  return (
    <div className="flex items-center gap-1.5">
      {HOURS.map((item) => (
        <Button key={item.hours} size="sm" variant={item.hours === 24 ? "primary" : "ghost"} loading={busy === item.hours} onClick={() => void grant(item.hours)}>
          允许 {item.label}
        </Button>
      ))}
    </div>
  );
}

/* What still blocks generation, most fundamental first; nothing is shown when the studio is ready. */
export function NodeNotice({ overview, className }: { overview?: StudioOverview; className?: string }) {
  if (!overview) return null;
  const { node, workflow, permissions } = overview;
  const notes: ReactNode[] = [];
  if (!workflow.available) notes.push(<Notice key="engine">{workflow.message}</Notice>);
  if (node.state === "unconfigured")
    notes.push(<Notice key="node">部署还没有指定生成节点。可以写剧本、拆分镜、上传素材和导出；出图、配音和动态需要生成节点。</Notice>);
  else if (node.state === "unpaired") notes.push(<Notice key="node">还没有配对生成节点。可以先写剧本、拆分镜，出图、配音和动态需要先在设备页配对节点。</Notice>);
  else if (node.state === "offline") notes.push(<Notice key="node">生成节点现在连不上。剧本和分镜照常可用，节点恢复后再生成。</Notice>);
  else if (permissions.needs_approval && permissions.tools.length > 0)
    notes.push(
      <Notice key="grant" action={permissions.can_grant ? <GrantButtons /> : undefined}>
        生成节点的制作工具每次运行前都要你批准。{permissions.can_grant ? "一键制作前可以先允许工作室在一段时间内直接使用它们，随时可撤销。" : "请让部署的所有者预先授权。"}
      </Notice>,
    );
  if (!overview.ffmpeg) notes.push(<Notice key="ffmpeg">控制面没有找到 FFmpeg，导出成片前需要安装 FFmpeg，或在部署配置里指定它的位置。</Notice>);
  if (!notes.length) return null;
  return <div className={cn("flex flex-col gap-2", className)}>{notes}</div>;
}

export const NODE_STATE: Record<StudioOverview["node"]["state"], { label: string; tone: "green" | "neutral" | "red" }> = {
  online: { label: "生成节点在线", tone: "green" },
  offline: { label: "生成节点离线", tone: "red" },
  unpaired: { label: "未配对生成节点", tone: "neutral" },
  unconfigured: { label: "未指定生成节点", tone: "neutral" },
};

function Poster({ item }: { item: ProjectSummary }) {
  const navigate = useNavigate();
  return (
    <button
      onClick={() => navigate(`/studio/${item.id}`)}
      className="group flex flex-col overflow-hidden rounded-[14px] border border-line bg-surface text-left transition-[box-shadow,transform] duration-200 hover:-translate-y-0.5 hover:shadow-[var(--shadow-pop)]"
    >
      <div className="relative bg-surface-2" style={{ aspectRatio: "4 / 5" }}>
        {item.cover ? (
          <img src={item.cover} alt="" loading="lazy" className="absolute inset-0 size-full object-cover" />
        ) : (
          <div className="absolute inset-0 flex flex-col justify-end p-5">
            <div className="mb-3 h-px w-10 bg-accent" />
            <div className="title-card line-clamp-3 text-[26px] leading-tight text-ink">{item.title}</div>
          </div>
        )}
        {item.cover && <div className="absolute inset-x-0 bottom-0 h-1/2 bg-gradient-to-t from-black/70 to-transparent" />}
        {item.cover && <div className="title-card absolute right-4 bottom-3.5 left-4 line-clamp-2 text-[22px] leading-tight text-white">{item.title}</div>}
        {item.active_jobs > 0 && (
          <span className="absolute top-3 left-3 inline-flex items-center gap-1.5 rounded-full bg-black/55 px-2.5 py-1 text-[11.5px] text-white backdrop-blur">
            <span className="animate-rim size-1.5 rounded-full bg-accent" />
            制作中 {item.active_jobs}
          </span>
        )}
        <span className="absolute top-3 right-3 rounded-full bg-black/45 px-2 py-0.5 text-[11px] text-white/90 backdrop-blur">{item.format}</span>
      </div>
      <div className="flex flex-1 flex-col gap-3 p-4">
        <p className="line-clamp-2 min-h-[2.6em] text-[13px] leading-relaxed text-ink-2">{item.logline || "还没有一句话简介"}</p>
        <StageMeter progress={item.progress} />
        <div className="flex items-center justify-between text-[12px] text-ink-3">
          <span>{item.progress.shots ? `${item.progress.shots} 镜　约 ${duration(item.progress.seconds)}` : "未拆分镜"}</span>
          <span>{relativeTime(item.updated_at)}</span>
        </div>
      </div>
    </button>
  );
}

const DURATIONS = [
  { value: "30", label: "30 秒" },
  { value: "60", label: "1 分钟" },
  { value: "90", label: "90 秒" },
  { value: "180", label: "3 分钟" },
  { value: "300", label: "5 分钟" },
];

export function NewProjectDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const navigate = useNavigate();
  const overview = useOverview();
  const [title, setTitle] = useState("");
  const [premise, setPremise] = useState("");
  const [format, setFormat] = useState<Project["format"]>("9:16");
  const [style, setStyle] = useState("anime-cel");
  const [seconds, setSeconds] = useState("60");
  const [genre, setGenre] = useState("");
  const [mode, setMode] = useState<"ai" | "paste">("ai");
  const [script, setScript] = useState("");
  const [busy, setBusy] = useState(false);

  async function create() {
    setBusy(true);
    try {
      const view = await api<ProjectView>("/v1/studio/projects", {
        body: { title, premise, format, style_id: style, target_seconds: Number(seconds), genre, script: mode === "paste" ? script : "" },
      });
      onOpenChange(false);
      navigate(`/studio/${view.project.id}/script`);
      if (mode === "ai" && premise.trim()) {
        await api(`/v1/studio/projects/${view.project.id}/director/script`, { body: {} });
      }
    } catch (error) {
      toast.error((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="新作品"
      description="先定好故事和画风，剧本、角色和分镜都可以之后再改。"
      width={640}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button variant="primary" loading={busy} disabled={!title.trim() || (mode === "paste" ? !script.trim() : false)} onClick={() => void create()}>
            {mode === "ai" && premise.trim() ? "创建并开始写剧本" : "创建"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-5 pb-2">
        <Field label="片名">
          <input className="field h-10" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="例如：最后一班车" autoFocus />
        </Field>
        <div className="flex flex-wrap items-end gap-x-6 gap-y-4">
          <Field label="画幅">
            <Segmented
              value={format}
              onChange={setFormat}
              options={[
                { value: "9:16", label: "竖屏", icon: <RectangleVertical className="size-3.5" /> },
                { value: "16:9", label: "横屏", icon: <RectangleHorizontal className="size-3.5" /> },
                { value: "1:1", label: "方屏", icon: <Square className="size-3.5" /> },
              ]}
            />
          </Field>
          <Field label="目标时长">
            <Segmented value={seconds} onChange={setSeconds} options={DURATIONS} />
          </Field>
        </div>
        <Field label="画风">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {(overview.data?.styles || []).map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setStyle(item.id)}
                className={cn(
                  "rounded-[10px] border px-3 py-2.5 text-left transition-colors",
                  style === item.id ? "border-accent bg-accent-soft" : "border-line hover:border-line-strong",
                )}
              >
                <div className={cn("text-[13.5px] font-medium", style === item.id && "text-accent-ink")}>{item.name}</div>
                <div className="mt-0.5 line-clamp-2 text-[12px] leading-snug text-ink-3">{item.description}</div>
              </button>
            ))}
          </div>
        </Field>
        <Field label="题材">
          <div className="flex flex-wrap gap-1.5">
            {GENRES.map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => setGenre(genre === item ? "" : item)}
                className={cn(
                  "h-7 rounded-full border px-3 text-[12.5px]",
                  genre === item ? "border-accent bg-accent-soft text-accent-ink" : "border-line text-ink-2 hover:border-line-strong",
                )}
              >
                {item}
              </button>
            ))}
          </div>
        </Field>
        <Segmented
          value={mode}
          onChange={setMode}
          className="self-start"
          options={[
            { value: "ai", label: "让 AI 写剧本" },
            { value: "paste", label: "我有剧本" },
          ]}
        />
        {mode === "ai" ? (
          <Field label="故事梗概" hint="写清楚主角、核心冲突和结局方向，一两句话就够。">
            <textarea className="field resize-y leading-relaxed" rows={4} value={premise} onChange={(e) => setPremise(e.target.value)} placeholder="例如：小镇车站的末班车晚点了，等车的女孩发现老站长每晚都在等同一个人……" />
          </Field>
        ) : (
          <Field label="剧本" hint="用「## 场景」分场，「角色（情绪）：台词」写对白，「【画面】」写动作。">
            <textarea className="field resize-y font-[inherit] leading-relaxed" rows={10} value={script} onChange={(e) => setScript(e.target.value)} />
          </Field>
        )}
      </div>
    </Dialog>
  );
}

export default function StudioHome() {
  const projects = useProjects();
  const overview = useOverview();
  const [creating, setCreating] = useState(false);
  const queue = overview.data?.queue;
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[1180px] px-6 py-7 md:px-10">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-[24px] font-semibold tracking-tight">工作室</h1>
            <p className="mt-1 text-[13.5px] text-ink-3">
              {queue && (queue.running.length || queue.queued || queue.waiting)
                ? `制作队列：${queue.running[0]?.label || "等待中"}${queue.queued ? `，还有 ${queue.queued} 项排队` : ""}${queue.waiting ? `，${queue.waiting} 项等你处理` : ""}，预计 ${duration(queue.eta_seconds)}`
                : "从一句梗概开始：写剧本、定角色、拆分镜、出画面、配音、合成成片。"}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {overview.data && (
              <Badge tone={NODE_STATE[overview.data.node.state].tone}>{NODE_STATE[overview.data.node.state].label}</Badge>
            )}
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setCreating(true)}>
              新作品
            </Button>
          </div>
        </div>
        <NodeNotice overview={overview.data} className="mt-5" />
        {projects.isLoading ? (
          <div className="flex justify-center py-20">
            <Spinner />
          </div>
        ) : projects.data?.length ? (
          <div className="mt-6 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
            {projects.data.map((item) => (
              <Poster key={item.id} item={item} />
            ))}
          </div>
        ) : (
          <Empty
            icon={<Clapperboard />}
            title="还没有作品"
            action={
              <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setCreating(true)}>
                新作品
              </Button>
            }
          >
            写一句梗概，AI 帮你写剧本、拆分镜；画面、配音和动作由已配对的生成节点制作。
          </Empty>
        )}
      </div>
      <NewProjectDialog open={creating} onOpenChange={setCreating} />
    </div>
  );
}

export { FORMAT_ASPECT };
