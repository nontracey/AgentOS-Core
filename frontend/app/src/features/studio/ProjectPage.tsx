import * as Popover from "@radix-ui/react-popover";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, ChevronRight, Film, ListOrdered, MoreHorizontal, RotateCcw, Settings2, Sparkles, Trash2, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { cn, duration, relativeTime } from "@/lib/format";
import { ACTIVE_JOB_STATES, type Job } from "@/lib/types";
import { Button, Dialog, Field, IconButton, Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger, ProgressBar, Select, Spinner, Switch } from "@/components/ui";
import { CastTab } from "./CastTab";
import { useOverview, useProject, type ProjectApi } from "./data";
import { FilmTab } from "./FilmTab";
import { jobKindLabel } from "./labels";
import { ScriptTab } from "./ScriptTab";
import { StoryboardTab } from "./StoryboardTab";
import { NodeNotice } from "./StudioHome";

const TABS = [
  { id: "script", label: "剧本" },
  { id: "cast", label: "角色与场景" },
  { id: "board", label: "分镜" },
  { id: "film", label: "成片" },
] as const;

const DOT: Record<Job["state"], string> = {
  queued: "bg-line-strong",
  running: "bg-accent",
  approval: "bg-accent",
  blocked: "bg-red",
  succeeded: "bg-green",
  failed: "bg-red",
  cancelled: "bg-ink-3",
  skipped: "bg-ink-3",
};

function RowAction({ label, onClick, children }: { label: string; onClick: () => void; children: ReactNode }) {
  return (
    <button onClick={onClick} aria-label={label} title={label} className="rounded p-0.5 text-ink-3 hover:bg-surface-3 hover:text-ink">
      {children}
    </button>
  );
}

function JobRow({ job, api: projectApi }: { job: Job; api: ProjectApi }) {
  const running = job.state === "running";
  const active = ACTIVE_JOB_STATES.includes(job.state);
  const retryable = job.state === "failed" || job.state === "cancelled" || job.state === "skipped";
  return (
    <div className={cn("rounded-[10px] px-3 py-2.5", running ? "bg-accent-soft" : "hover:bg-surface-2")}>
      <div className="flex items-center gap-2">
        {running ? <Spinner className="size-3.5 text-accent-ink" /> : <span className={cn("size-1.5 shrink-0 rounded-full", DOT[job.state])} />}
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium">{job.label}</span>
        <span className="text-[11.5px] text-ink-3">{jobKindLabel(job.kind)}</span>
        {active && (
          <RowAction label={job.batch_size > 1 ? "取消这一批未完成的项目" : "取消"} onClick={() => void projectApi.cancelJob(job.id)}>
            <X className="size-3.5" />
          </RowAction>
        )}
        {retryable && (
          <RowAction label="重试" onClick={() => void projectApi.retryJob(job.id)}>
            <RotateCcw className="size-3.5" />
          </RowAction>
        )}
      </div>
      {running && (
        <div className="mt-2">
          <div className="mb-1 truncate text-[12px] text-accent-ink">{job.progress.stage || "进行中"}</div>
          {typeof job.progress.fraction === "number" ? (
            <ProgressBar value={job.progress.fraction} />
          ) : job.progress.total ? (
            <ProgressBar value={(job.progress.done || 0) / job.progress.total} />
          ) : (
            <ProgressBar value={0.35} live />
          )}
        </div>
      )}
      {job.state === "approval" && (
        <div className="mt-2 flex items-center justify-between gap-3">
          <span className="text-[12px] leading-snug text-ink-2">等你批准后才会交给生成节点</span>
          <Button size="sm" variant="primary" icon={<Check className="size-3.5" />} onClick={() => void projectApi.approveJob(job)}>
            批准
          </Button>
        </div>
      )}
      {job.state === "blocked" && (
        <div className="mt-1.5 flex flex-col gap-2">
          <div className="text-[12px] leading-snug text-red">{job.error}</div>
          <Button size="sm" className="self-start" icon={<RotateCcw className="size-3.5" />} onClick={() => void projectApi.reconcileJob(job)}>
            核对并继续
          </Button>
        </div>
      )}
      {(job.state === "failed" || job.state === "skipped") && job.error && (
        <div className="mt-1 text-[12px] leading-snug text-red">{job.error}</div>
      )}
      {retryable && job.batch_size > 1 && job.gpu && (
        <button className="mt-1 text-[12px] text-accent-ink hover:underline" onClick={() => void projectApi.retryJob(job.id, true)}>
          重试这一批没完成的项目
        </button>
      )}
      {job.state === "succeeded" && job.result?.skipped?.length > 0 && (
        <div className="mt-1 text-[12px] leading-snug text-ink-3">{job.result.skipped.join("；")}</div>
      )}
    </div>
  );
}

function QueueButton({ projectApi }: { projectApi: ProjectApi }) {
  const jobs = projectApi.view?.jobs || [];
  const active = projectApi.activeJobs;
  const running = active.find((j) => j.state === "running");
  const remaining = active.reduce((sum, job) => sum + (job.state === "running" ? job.estimate_seconds / 2 : job.estimate_seconds), 0);
  const waiting = active.filter((j) => j.state === "approval" || j.state === "blocked").length;
  const recent = jobs.filter((j) => !ACTIVE_JOB_STATES.includes(j.state)).slice(0, 12);
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button
          className={cn(
            "inline-flex h-9 items-center gap-2 rounded-[9px] px-3 text-[13px] font-medium transition-colors",
            running ? "bg-accent-soft text-accent-ink" : "text-ink-2 hover:bg-surface-3 hover:text-ink",
          )}
        >
          {running ? <Spinner className="size-3.5 text-accent-ink" /> : <ListOrdered className="size-4" />}
          <span className="hidden max-w-48 truncate sm:inline">{running ? running.label : "任务队列"}</span>
          {active.length > 0 && <span className="tabular rounded-full bg-surface px-1.5 text-[11.5px]">{active.length}</span>}
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content align="end" sideOffset={6} className="z-50 w-[min(92vw,380px)] rounded-[14px] border border-line bg-surface shadow-[var(--shadow-pop)]">
          <div className="flex items-center justify-between border-b border-line px-4 py-3">
            <span className="text-[14px] font-semibold">任务队列</span>
            <span className="text-[12px] text-ink-3">
              {active.length ? `${waiting ? `${waiting} 项等你处理，` : ""}剩余约 ${duration(remaining)}` : "全部完成"}
            </span>
          </div>
          <div className="max-h-[60vh] overflow-y-auto p-2">
            {active.map((job) => (
              <JobRow key={job.id} job={job} api={projectApi} />
            ))}
            {recent.length > 0 && <div className="px-3 pt-3 pb-1 text-[12px] text-ink-3">最近完成</div>}
            {recent.map((job) => (
              <JobRow key={job.id} job={job} api={projectApi} />
            ))}
            {!jobs.length && <div className="px-3 py-8 text-center text-[13px] text-ink-3">还没有任务</div>}
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function SettingsDialog({ projectApi, open, onOpenChange }: { projectApi: ProjectApi; open: boolean; onOpenChange: (v: boolean) => void }) {
  const overview = useOverview();
  const project = projectApi.project;
  if (!project) return null;
  const engines = overview.data?.engines || [];
  const options = (kind: string) =>
    engines
      .filter((e) => e.kind === kind)
      .map((e) => ({ value: e.id, label: e.label, hint: e.available ? e.note : `${e.reason}` }));
  const setEngine = (kind: "image" | "video" | "voice" | "lipsync", value: string) =>
    projectApi.edit((p) => ({ ...p, engines: { ...p.engines, [kind]: value } }));
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="项目设置" description="生成引擎和画风对之后的新生成生效，已有结果不变。" width={600}>
      <div className="flex flex-col gap-5 pb-3">
        <Field label="出图引擎">
          <Select value={project.engines.image} onChange={(v) => setEngine("image", v)} options={options("image")} />
        </Field>
        <Field label="视频引擎">
          <Select value={project.engines.video} onChange={(v) => setEngine("video", v)} options={options("video")} />
        </Field>
        <Field label="画风" hint="选择预设，或写自己的英文画风提示词（会替代预设）。">
          <Select value={project.style_id} onChange={(v) => projectApi.edit((p) => ({ ...p, style_id: v }))} options={(overview.data?.styles || []).map((s) => ({ value: s.id, label: s.name, hint: s.description }))} />
        </Field>
        <Field label="自定义画风提示词（英文，可选）">
          <textarea className="field resize-y text-[13px] leading-relaxed" rows={3} value={project.style_prompt} onChange={(e) => projectApi.edit((p) => ({ ...p, style_prompt: e.target.value }))} placeholder={overview.data?.styles.find((s) => s.id === project.style_id)?.prompt} />
        </Field>
        <Field label="不想出现的内容（英文，可选）">
          <textarea className="field resize-y text-[13px] leading-relaxed" rows={2} value={project.style_negative} onChange={(e) => projectApi.edit((p) => ({ ...p, style_negative: e.target.value }))} placeholder={overview.data?.styles.find((s) => s.id === project.style_id)?.negative} />
        </Field>
        <label className="flex items-center justify-between gap-4">
          <span>
            <span className="block text-[13.5px] font-medium">动作补帧（实验）</span>
            <span className="block text-[12px] text-ink-3">把 16 帧视频插值到 30 帧更顺滑；动作快时可能扭曲，先用预览看效果。</span>
          </span>
          <Switch checked={project.smooth_motion} onChange={(v) => projectApi.edit((p) => ({ ...p, smooth_motion: v }))} />
        </label>
      </div>
    </Dialog>
  );
}

function AutopilotDialog({ projectApi, open, onOpenChange }: { projectApi: ProjectApi; open: boolean; onOpenChange: (v: boolean) => void }) {
  const view = projectApi.view;
  const [quality, setQuality] = useState<"preview" | "final">("preview");
  if (!view) return null;
  const p = view.progress;
  const missingVoices = (projectApi.project?.characters || []).filter((c) => !view.selections[`voice_ref:${c.id}`]);
  const steps = [
    { label: "剧本", done: Boolean(projectApi.project?.script.trim()), note: projectApi.project?.script.trim() ? "已有剧本" : "按梗概写剧本" },
    { label: "拆分镜", done: p.shots > 0, note: p.shots ? `${p.shots} 个镜头` : "拆成镜头、角色和场景" },
    { label: "角色与场景定妆", done: p.characters > 0 && p.refs >= p.characters && p.loc_refs >= p.locations, note: `缺 ${Math.max(0, p.characters - p.refs) + Math.max(0, p.locations - p.loc_refs)} 张` },
    { label: "配音", done: p.lines > 0 && p.voices >= p.lines, note: p.lines ? `${p.voices}/${p.lines} 句` : "拆完分镜后计算" },
    { label: "分镜画面", done: p.shots > 0 && p.frames >= p.shots, note: p.shots ? `${p.frames}/${p.shots} 镜` : "拆完分镜后计算" },
    { label: "动态镜头", done: p.moving > 0 ? p.clips >= p.moving : p.shots > 0, note: p.moving ? `${p.clips}/${p.moving} 镜` : "没有需要图生视频的镜头" },
    { label: quality === "final" ? "导出 1080p 成片" : "合成预览", done: false, note: "" },
  ];
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="一键制作"
      description="按顺序补齐缺的部分，已经选定的结果不会重做。每一步都会自动选第一个结果，之后可以逐镜替换。"
      width={540}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            variant="accent"
            icon={<Sparkles className="size-4" />}
            onClick={() => {
              onOpenChange(false);
              void projectApi.autopilot({ quality });
            }}
          >
            开始制作
          </Button>
        </>
      }
    >
      <ol className="flex flex-col gap-1 pb-2">
        {steps.map((step, index) => (
          <li key={step.label} className="flex items-center gap-3 rounded-[10px] px-2 py-2">
            <span className={cn("tabular flex size-6 shrink-0 items-center justify-center rounded-full text-[12px] font-semibold", step.done ? "bg-green-soft text-green" : "bg-surface-3 text-ink-2")}>
              {step.done ? <Check className="size-3.5" /> : index + 1}
            </span>
            <span className="flex-1 text-[14px]">{step.label}</span>
            <span className="text-[12.5px] text-ink-3">{step.done ? "已完成" : step.note}</span>
          </li>
        ))}
      </ol>
      {missingVoices.length > 0 && (
        <div className="mb-3 rounded-[10px] bg-surface-2 px-3.5 py-2.5 text-[12.5px] leading-relaxed text-ink-2">
          {missingVoices.map((c) => c.name).join("、")} 还没有参考音，这些角色的台词会先跳过配音（字幕照常）。在「角色与场景」里给他们上传 5–15 秒的干净人声。
        </div>
      )}
      <div className="mb-2 flex items-center justify-between gap-4 rounded-[10px] border border-line px-3.5 py-2.5">
        <span className="text-[13.5px]">最后一步</span>
        <div className="flex gap-1">
          {(["preview", "final"] as const).map((value) => (
            <button key={value} onClick={() => setQuality(value)} className={cn("h-7 rounded-[8px] px-3 text-[12.5px]", quality === value ? "bg-accent-soft font-medium text-accent-ink" : "text-ink-2 hover:bg-surface-3")}>
              {value === "preview" ? "预览（快）" : "1080p 成片"}
            </button>
          ))}
        </div>
      </div>
      <p className="text-[12px] leading-relaxed text-ink-3">按当前缺口估算生成时间约 {duration(view.estimate.seconds)}；拆完分镜后会更准。可以随时关掉页面，制作在后台继续。</p>
    </Dialog>
  );
}

export default function ProjectPage() {
  const { id = "", tab = "script" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const projectApi = useProject(id);
  const overview = useOverview();
  const [settings, setSettings] = useState(false);
  const [autopilot, setAutopilot] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const project = projectApi.project;
  const view = projectApi.view;

  if (projectApi.loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }
  if (!project || !view) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-[14px] text-ink-3">
        作品不存在或已删除
        <Button onClick={() => navigate("/studio")}>回到工作室</Button>
      </div>
    );
  }
  const p = view.progress;
  const tabState: Record<string, string> = {
    script: project.script.trim() ? `${project.script.length} 字` : "",
    cast: p.characters ? `${p.refs}/${p.characters}` : "",
    board: p.shots ? `${p.frames}/${p.shots}` : "",
    film: p.rendered ? "已合成" : "",
  };

  return (
    <div className="flex h-full flex-col">
      <header className="shrink-0 border-b border-line px-4 pt-3 md:px-6">
        <div className="flex items-center gap-2">
          <IconButton label="回到工作室" onClick={() => navigate("/studio")}>
            <ArrowLeft className="size-[18px]" />
          </IconButton>
          <div className="min-w-0 flex-1">
            {renaming ? (
              <input
                autoFocus
                defaultValue={project.title}
                onBlur={(e) => {
                  setRenaming(false);
                  const value = e.target.value.trim();
                  if (value && value !== project.title) projectApi.edit((current) => ({ ...current, title: value }));
                }}
                onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                className="field title-card h-9 max-w-md text-[20px]"
              />
            ) : (
              <button onClick={() => setRenaming(true)} className="title-card max-w-full truncate rounded-md px-1 text-left text-[22px] leading-9 hover:bg-surface-2">
                {project.title}
              </button>
            )}
          </div>
          <span className="hidden text-[12px] text-ink-3 md:inline">{projectApi.saving ? "保存中…" : projectApi.dirty ? "未保存" : `已保存　${relativeTime(project.updated_at)}`}</span>
          <QueueButton projectApi={projectApi} />
          <Button variant="accent" icon={<Sparkles className="size-4" />} onClick={() => setAutopilot(true)} className="hidden sm:inline-flex">
            一键制作
          </Button>
          <Menu>
            <MenuTrigger asChild>
              <button aria-label="更多" className="rounded-[8px] p-2 text-ink-2 hover:bg-surface-3 hover:text-ink">
                <MoreHorizontal className="size-[18px]" />
              </button>
            </MenuTrigger>
            <MenuContent>
              <MenuItem icon={<Sparkles />} onSelect={() => setAutopilot(true)}>
                一键制作
              </MenuItem>
              <MenuItem icon={<Film />} onSelect={() => void projectApi.render("final")}>
                导出 1080p 成片
              </MenuItem>
              <MenuItem icon={<Settings2 />} onSelect={() => setSettings(true)}>
                引擎与画风
              </MenuItem>
              <MenuSeparator />
              <MenuItem
                icon={<Trash2 />}
                danger
                onSelect={() => {
                  if (!window.confirm(`删除「${project.title}」？已生成的素材会保留在成果库。`)) return;
                  void api(`/v1/studio/projects/${id}`, { method: "DELETE" }).then(() => {
                    void qc.invalidateQueries({ queryKey: ["studio", "projects"] });
                    toast.success("已删除");
                    navigate("/studio");
                  });
                }}
              >
                删除作品
              </MenuItem>
            </MenuContent>
          </Menu>
        </div>
        <nav className="-mb-px mt-1 flex gap-1 overflow-x-auto">
          {TABS.map((item, index) => (
            <button
              key={item.id}
              onClick={() => navigate(`/studio/${id}/${item.id}`)}
              className={cn(
                "flex h-10 shrink-0 items-center gap-2 border-b-2 px-3 text-[14px] transition-colors",
                tab === item.id ? "border-accent font-medium text-ink" : "border-transparent text-ink-3 hover:text-ink",
              )}
            >
              <span className={cn("tabular flex size-5 items-center justify-center rounded-full text-[11px]", tab === item.id ? "bg-accent text-on-accent" : "bg-surface-3")}>{index + 1}</span>
              {item.label}
              {tabState[item.id] && <span className="tabular text-[12px] text-ink-3">{tabState[item.id]}</span>}
              {index < TABS.length - 1 && <ChevronRight className="ml-1 hidden size-3.5 text-line-strong lg:block" />}
            </button>
          ))}
        </nav>
      </header>
      <NodeNotice overview={overview.data} className="shrink-0 px-4 pt-3 md:px-6" />
      <div className="min-h-0 flex-1">
        {tab === "script" && <ScriptTab projectApi={projectApi} />}
        {tab === "cast" && <CastTab projectApi={projectApi} />}
        {tab === "board" && <StoryboardTab projectApi={projectApi} />}
        {tab === "film" && <FilmTab projectApi={projectApi} />}
      </div>
      <SettingsDialog projectApi={projectApi} open={settings} onOpenChange={setSettings} />
      <AutopilotDialog projectApi={projectApi} open={autopilot} onOpenChange={setAutopilot} />
    </div>
  );
}
