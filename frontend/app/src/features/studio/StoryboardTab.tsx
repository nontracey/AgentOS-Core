import { AlertTriangle, Clapperboard, Film, LayoutGrid, Mic, Plus, Rows3, Sparkles, Wand2, X } from "lucide-react";
import { Fragment, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { cn, duration, sheetTime } from "@/lib/format";
import type { Shot } from "@/lib/types";
import { useIsPhone } from "@/hooks/useMedia";
import { Button, Empty, Segmented } from "@/components/ui";
import { newId, type ProjectApi } from "./data";
import { CAMERAS, characterColor, EMOTION_TONES, FORMAT_ASPECT, MOTIONS, TRANSITIONS } from "./labels";
import { AudioButton, FrameBox, JobBadge } from "./media";
import { ShotInspector } from "./ShotInspector";

type Filter = "all" | "frame" | "voice" | "motion";
type View = "sheet" | "list";

const VIEW_KEY = "agentos_board_view";

function frameWidth(format: string): number {
  return format === "16:9" ? 168 : format === "1:1" ? 104 : 72;
}

function tileMin(format: string): number {
  return format === "16:9" ? 260 : format === "1:1" ? 190 : 166;
}

/** "场1 学院庭院 · 白天" → ["场1", "学院庭院 · 白天"] */
function sceneParts(scene: string): [string, string] {
  const match = scene.trim().match(/^(第?[\d一二三四五六七八九十百]+[场幕]|场\s*\d+)\s*(.*)$/);
  return match ? [match[1].replace(/\s+/g, ""), match[2]] : ["", scene.trim()];
}

function ShotTile({
  shot,
  index,
  projectApi,
  active,
  checked,
  onOpen,
  onCheck,
  warnings,
}: {
  shot: Shot;
  index: number;
  projectApi: ProjectApi;
  active: boolean;
  checked: boolean;
  onOpen: () => void;
  onCheck: (value: boolean) => void;
  warnings: string[];
}) {
  const project = projectApi.project!;
  const frame = projectApi.selected("frame", shot.id);
  const clip = projectApi.selected("clip", shot.id);
  const jobs = [...projectApi.jobsFor("frame", shot.id), ...projectApi.jobsFor("clip", shot.id)];
  const live = jobs.some((j) => j.state === "running");
  const waiting = jobs.some((j) => j.state === "queued");
  const camera = CAMERAS.find((c) => c.value === shot.camera)?.label;
  const motion = MOTIONS.find((m) => m.value === shot.motion);
  const voiced = shot.lines.filter((line) => projectApi.selected("voice", line.id)).length;
  const first = shot.lines[0];
  const speakerIndex = first ? project.characters.findIndex((c) => c.id === first.character_id) : -1;
  const speaker = speakerIndex >= 0 ? project.characters[speakerIndex] : undefined;
  return (
    <div className="group/tile min-w-0">
      <FrameBox
        src={clip?.thumb || frame?.thumb}
        aspect={FORMAT_ASPECT[project.format]}
        live={live}
        onClick={onOpen}
        className={cn("transition-shadow", active ? "shadow-[0_0_0_2px_var(--accent)]" : checked ? "shadow-[0_0_0_2px_var(--accent-strong)]" : "hover:border-line-strong")}
      >
        {!frame && !live && !waiting && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              void projectApi.generate("frame", [shot.id]);
            }}
            className="absolute inset-0 flex flex-col items-center justify-center gap-1 text-[12px] text-ink-3 hover:text-ink"
          >
            <Sparkles className="size-4" />
            生成画面
          </button>
        )}
        <span className="tabular pointer-events-none absolute top-1.5 left-1.5 rounded-[5px] bg-black/60 px-1.5 text-[11.5px] leading-[19px] font-semibold text-white">
          C{String(index + 1).padStart(2, "0")}
        </span>
        <label
          onClick={(e) => e.stopPropagation()}
          className={cn(
            "absolute top-1 right-1 flex size-7 items-center justify-center rounded-[6px] transition-opacity",
            checked ? "opacity-100" : "opacity-0 group-hover/tile:opacity-100 focus-within:opacity-100",
          )}
        >
          <input type="checkbox" checked={checked} onChange={(e) => onCheck(e.target.checked)} className="size-4 accent-[var(--accent)]" aria-label={`选择第 ${index + 1} 镜`} />
        </label>
        <div className="absolute bottom-1.5 left-1.5 flex items-center gap-1">
          <JobBadge jobs={jobs} />
          {shot.motion !== "still" && !live && (
            <span className={cn("inline-flex items-center gap-1 rounded-full px-1.5 py-px text-[10.5px] font-medium", clip ? "bg-green text-white" : "bg-black/60 text-white")}>
              <Film className="size-3" />
              {clip ? "动态" : "待动态"}
            </span>
          )}
        </div>
        <span className="tabular pointer-events-none absolute right-1.5 bottom-1.5 rounded-[5px] bg-black/60 px-1.5 text-[11px] leading-[18px] text-white">{sheetTime(shot.duration)}</span>
      </FrameBox>
      <button type="button" onClick={onOpen} className="mt-2 block w-full text-left">
        <p className={cn("line-clamp-2 text-[12.5px] leading-[1.55]", shot.action ? "text-ink" : "text-ink-3")}>{shot.action || "还没写画面动作"}</p>
        {first ? (
          <p className="mt-1 line-clamp-2 text-[12.5px] leading-[1.55] text-ink-2">
            <span className="font-medium" style={{ color: speaker ? speaker.color || characterColor(speakerIndex) : undefined }}>
              {speaker?.name || "旁白"}
            </span>
            <span className="text-ink-3">：</span>
            {first.text}
            {shot.lines.length > 1 && <span className="text-ink-3">（另 {shot.lines.length - 1} 句）</span>}
          </p>
        ) : null}
        <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11.5px] text-ink-3">
          <span>{shot.size}</span>
          <span>{shot.motion === "still" ? camera : motion?.label}</span>
          {shot.lines.length > 0 && (
            <span className={cn("inline-flex items-center gap-0.5", voiced === shot.lines.length ? "text-green" : "")}>
              <Mic className="size-3" />
              {voiced}/{shot.lines.length}
            </span>
          )}
          {warnings.length > 0 && (
            <span className="inline-flex items-center gap-0.5 text-accent-ink" title={warnings.join("\n")}>
              <AlertTriangle className="size-3" />
              {warnings.length}
            </span>
          )}
        </div>
      </button>
    </div>
  );
}

function ShotRow({
  shot,
  index,
  projectApi,
  active,
  checked,
  onOpen,
  onCheck,
  warnings,
}: {
  shot: Shot;
  index: number;
  projectApi: ProjectApi;
  active: boolean;
  checked: boolean;
  onOpen: () => void;
  onCheck: (value: boolean) => void;
  warnings: string[];
}) {
  const project = projectApi.project!;
  const frame = projectApi.selected("frame", shot.id);
  const clip = projectApi.selected("clip", shot.id);
  const frameJobs = projectApi.jobsFor("frame", shot.id);
  const clipJobs = projectApi.jobsFor("clip", shot.id);
  const live = [...frameJobs, ...clipJobs].some((j) => j.state === "running");
  const location = project.locations.find((l) => l.id === shot.location_id);
  const camera = CAMERAS.find((c) => c.value === shot.camera)?.label;
  const motion = MOTIONS.find((m) => m.value === shot.motion);
  const transition = TRANSITIONS.find((t) => t.value === shot.transition);
  const width = frameWidth(project.format);
  return (
    <div
      onClick={onOpen}
      className={cn(
        "group grid cursor-pointer grid-cols-[64px_auto_minmax(0,1.25fr)_minmax(0,1fr)_76px] border-b border-line transition-colors",
        active ? "bg-accent-soft/50" : "hover:bg-surface-2/70",
      )}
    >
      <div className="relative border-r border-[var(--sheet-guide)] px-3 py-3">
        <div className="tabular text-[15px] leading-none font-semibold">C{String(index + 1).padStart(2, "0")}</div>
        <label onClick={(e) => e.stopPropagation()} className={cn("mt-3 flex items-center", checked ? "opacity-100" : "opacity-0 group-hover:opacity-100")}>
          <input type="checkbox" checked={checked} onChange={(e) => onCheck(e.target.checked)} className="size-4 accent-[var(--accent)]" aria-label={`选择第 ${index + 1} 镜`} />
        </label>
      </div>
      <div className="border-r border-[var(--sheet-guide)] p-2.5" style={{ width: width + 20 }}>
        <FrameBox src={clip ? clip.thumb || frame?.thumb : frame?.thumb} aspect={FORMAT_ASPECT[project.format]} live={live}>
          {!frame && !live && frameJobs.every((j) => j.state !== "queued") && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                void projectApi.generate("frame", [shot.id]);
              }}
              className="absolute inset-0 flex flex-col items-center justify-center gap-1 text-[12px] text-ink-3 hover:text-ink"
            >
              <Sparkles className="size-4" />
              生成画面
            </button>
          )}
          <div className="absolute top-1.5 left-1.5 flex flex-col items-start gap-1">
            <JobBadge jobs={[...frameJobs, ...clipJobs]} />
          </div>
          {shot.motion !== "still" && (
            <span className={cn("absolute right-1.5 bottom-1.5 inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10.5px] font-medium", clip ? "bg-green text-white" : "bg-black/55 text-white")}>
              <Film className="size-3" />
              {clip ? "动态" : "待生成"}
            </span>
          )}
        </FrameBox>
      </div>
      <div className="min-w-0 border-r border-[var(--sheet-guide)] px-3.5 py-3">
        <p className={cn("line-clamp-4 text-[13.5px] leading-relaxed", !shot.action && "text-ink-3")}>{shot.action || "还没写画面动作"}</p>
        <div className="mt-2 flex flex-wrap gap-1.5 text-[11.5px]">
          <span className="rounded-[5px] bg-surface-3 px-1.5 py-0.5 text-ink-2">{shot.size}</span>
          <span className="rounded-[5px] bg-surface-3 px-1.5 py-0.5 text-ink-2">{shot.motion === "still" ? camera : motion?.label}</span>
          {location && <span className="rounded-[5px] bg-surface-3 px-1.5 py-0.5 text-ink-2">{location.name}</span>}
          {shot.transition !== "cut" && <span className="rounded-[5px] bg-blue-soft px-1.5 py-0.5 text-blue">{transition?.label}</span>}
        </div>
        {warnings.map((warning) => (
          <div key={warning} className="mt-2 flex items-start gap-1.5 text-[12px] leading-snug text-accent-ink">
            <AlertTriangle className="mt-px size-3.5 shrink-0" />
            {warning.replace(/^第 \d+ 镜/, "")}
          </div>
        ))}
      </div>
      <div className="min-w-0 border-r border-[var(--sheet-guide)] px-3.5 py-3">
        {shot.lines.length === 0 && <span className="text-[13px] text-ink-3">无台词</span>}
        <div className="flex flex-col gap-2">
          {shot.lines.map((line) => {
            const characterIndex = project.characters.findIndex((c) => c.id === line.character_id);
            const character = project.characters[characterIndex];
            const voice = projectApi.selected("voice", line.id);
            const jobs = projectApi.jobsFor("voice", line.id);
            return (
              <div key={line.id} className="flex items-start gap-2">
                {voice ? (
                  <AudioButton url={voice.url} size="sm" className="mt-0.5" />
                ) : (
                  <span className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full border border-dashed border-line-strong text-ink-3">
                    <Mic className="size-3" />
                  </span>
                )}
                <div className="min-w-0 text-[13px] leading-relaxed">
                  <span className="font-medium" style={{ color: character ? character.color || characterColor(characterIndex) : undefined }}>
                    {character?.name || "旁白"}
                  </span>
                  <span className={cn("ml-1 text-[11.5px]", EMOTION_TONES[line.emotion] || "text-ink-3")}>{line.emotion !== "平静" && line.emotion}</span>
                  <span className="ml-1.5">{line.text}</span>
                  <JobBadge jobs={jobs} />
                </div>
              </div>
            );
          })}
        </div>
      </div>
      <div className="px-3 py-3 text-right">
        <div className="tabular text-[16px] font-semibold">{sheetTime(shot.duration)}</div>
        <div className="tabular text-[11.5px] text-ink-3">{shot.duration.toFixed(1)} 秒</div>
      </div>
    </div>
  );
}

function ShotCard({ shot, index, projectApi, onOpen }: { shot: Shot; index: number; projectApi: ProjectApi; onOpen: () => void }) {
  const project = projectApi.project!;
  const frame = projectApi.selected("frame", shot.id);
  const jobs = [...projectApi.jobsFor("frame", shot.id), ...projectApi.jobsFor("clip", shot.id)];
  return (
    <button onClick={onOpen} className="flex w-full gap-3 border-b border-line px-4 py-3 text-left">
      <div className="w-[84px] shrink-0">
        <FrameBox src={frame?.thumb} aspect={FORMAT_ASPECT[project.format]} live={jobs.some((j) => j.state === "running")} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="tabular text-[14px] font-semibold">C{String(index + 1).padStart(2, "0")}</span>
          <span className="text-[12px] text-ink-3">{shot.size}</span>
          <span className="tabular ml-auto text-[12.5px] text-ink-2">{sheetTime(shot.duration)}</span>
        </div>
        <p className="mt-1 line-clamp-2 text-[13px] leading-relaxed text-ink-2">{shot.action || "还没写画面动作"}</p>
        {shot.lines[0] && <p className="mt-1 line-clamp-1 text-[12.5px] text-ink-3">「{shot.lines[0].text}」</p>}
        <div className="mt-1">
          <JobBadge jobs={jobs} />
        </div>
      </div>
    </button>
  );
}

export function StoryboardTab({ projectApi }: { projectApi: ProjectApi }) {
  const navigate = useNavigate();
  const phone = useIsPhone();
  const project = projectApi.project!;
  const view = projectApi.view!;
  const [openId, setOpenId] = useState<string | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<Filter>("all");
  const [layout, setLayout] = useState<View>(() => {
    try {
      return localStorage.getItem(VIEW_KEY) === "list" ? "list" : "sheet";
    } catch {
      return "sheet";
    }
  });
  const changeLayout = (next: View) => {
    setLayout(next);
    try {
      localStorage.setItem(VIEW_KEY, next);
    } catch {
      /* per-browser preference only */
    }
  };
  const warnings = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const w of view.warnings) {
      if (!w.shot_id) continue;
      map.set(w.shot_id, [...(map.get(w.shot_id) || []), w.message]);
    }
    return map;
  }, [view.warnings]);
  const total = project.shots.reduce((sum, s) => sum + s.duration, 0);
  const shots = project.shots
    .map((shot, index) => ({ shot, index }))
    .filter(({ shot }) => {
      if (filter === "frame") return !projectApi.selected("frame", shot.id);
      if (filter === "voice") return shot.lines.some((l) => !projectApi.selected("voice", l.id));
      if (filter === "motion") return shot.motion !== "still";
      return true;
    });
  const globalWarnings = view.warnings.filter((w) => !w.shot_id);
  const selectedIds = [...checked];
  const targets = selectedIds.length ? selectedIds : shots.map((s) => s.shot.id);
  const lineTargets = project.shots.filter((s) => targets.includes(s.id)).flatMap((s) => s.lines.map((l) => l.id));

  if (!project.shots.length) {
    return (
      <Empty
        icon={<Clapperboard />}
        title="还没有分镜"
        action={
          <div className="flex gap-2">
            <Button variant="primary" onClick={() => navigate(`/studio/${project.id}/script`)}>
              去写剧本并拆分镜
            </Button>
            <Button
              onClick={() =>
                projectApi.edit((p) => ({
                  ...p,
                  shots: [{ id: newId("shot"), scene: "", location_id: null, character_ids: [], size: "中景", camera: "push_in", action: "", image_prompt: "", motion_prompt: "", lines: [], duration: 3, motion: "still", transition: "cut", lipsync: false, face_box: null, seed: null, notes: "" }],
                }))
              }
            >
              手动添加镜头
            </Button>
          </div>
        }
      >
        在剧本页点「拆分镜」，AI 会把剧本拆成镜头，每一镜写好景别、动作、台词和时长。
      </Empty>
    );
  }

  const openShot = project.shots.find((s) => s.id === openId);
  return (
    <div className="flex h-full">
      <div className={cn("flex min-w-0 flex-1 flex-col", openShot && !phone && "border-r border-line")}>
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-line px-4 py-2.5 md:px-5">
          <span className="tabular text-[13px] text-ink-2">
            {project.shots.length} 镜　{duration(total)}
          </span>
          <Segmented
            size="sm"
            value={filter}
            onChange={setFilter}
            options={[
              { value: "all", label: "全部" },
              { value: "frame", label: "缺画面" },
              { value: "voice", label: "缺配音" },
              { value: "motion", label: "动态镜头" },
            ]}
          />
          <Segmented
            size="sm"
            value={layout}
            onChange={changeLayout}
            options={[
              { value: "sheet", label: <span className="sr-only">画板</span>, icon: <LayoutGrid className="size-3.5" aria-hidden /> },
              { value: "list", label: <span className="sr-only">列表</span>, icon: <Rows3 className="size-3.5" aria-hidden /> },
            ]}
          />
          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            {selectedIds.length > 0 && (
              <span className="flex items-center gap-1 text-[12.5px] text-accent-ink">
                已选 {selectedIds.length} 镜
                <button onClick={() => setChecked(new Set())} aria-label="清除选择" className="rounded p-0.5 hover:bg-surface-3">
                  <X className="size-3.5" />
                </button>
              </span>
            )}
            <Button size="sm" icon={<Sparkles className="size-3.5" />} onClick={() => void projectApi.generate("frame", targets.filter((id) => selectedIds.length || !projectApi.selected("frame", id)))}>
              {selectedIds.length ? "生成画面" : "补齐画面"}
            </Button>
            <Button size="sm" icon={<Mic className="size-3.5" />} disabled={!lineTargets.length} onClick={() => void projectApi.generate("voice", lineTargets.filter((id) => selectedIds.length || !projectApi.selected("voice", id)))}>
              {selectedIds.length ? "生成配音" : "补齐配音"}
            </Button>
            <Button
              size="sm"
              icon={<Film className="size-3.5" />}
              onClick={() =>
                void projectApi.generate(
                  "clip",
                  project.shots.filter((s) => targets.includes(s.id) && s.motion !== "still" && (selectedIds.length || !projectApi.selected("clip", s.id))).map((s) => s.id),
                )
              }
            >
              生成动态
            </Button>
            <Button size="sm" variant="ghost" icon={<Wand2 className="size-3.5" />} onClick={() => void projectApi.director("prompts", { shot_ids: selectedIds.length ? selectedIds : undefined })}>
              AI 改写提示词
            </Button>
          </div>
        </div>
        {globalWarnings.map((w) => (
          <div key={w.message} className="flex items-center gap-2 border-b border-line bg-accent-soft/40 px-5 py-2 text-[12.5px] text-accent-ink">
            <AlertTriangle className="size-3.5" />
            {w.message}
          </div>
        ))}
        <div className="min-h-0 flex-1 overflow-y-auto">
          {phone && layout === "list" ? (
            shots.map(({ shot, index }) => <ShotCard key={shot.id} shot={shot} index={index} projectApi={projectApi} onOpen={() => setOpenId(shot.id)} />)
          ) : phone || layout === "sheet" ? (
            <div className="grid gap-x-4 gap-y-6 px-4 pt-4 pb-2 md:px-5" style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${phone ? 140 : tileMin(project.format)}px, 1fr))` }}>
              {shots.map(({ shot, index }, position) => {
                const previous = shots[position - 1]?.shot;
                const startsScene = !previous || previous.scene !== shot.scene;
                const [number, place] = sceneParts(shot.scene);
                const sceneShots = startsScene ? project.shots.filter((s) => s.scene === shot.scene) : [];
                return (
                  <Fragment key={shot.id}>
                    {startsScene && shot.scene && (
                      <div className={cn("col-span-full flex items-baseline gap-3", position > 0 && "mt-3")}>
                        {number && <span className="title-card text-[18px] leading-none">{number}</span>}
                        <span className="text-[13.5px] font-medium text-ink-2">{place}</span>
                        <span className="tabular text-[12px] text-ink-3">
                          {sceneShots.length} 镜　{duration(sceneShots.reduce((sum, s) => sum + s.duration, 0))}
                        </span>
                        <span className="h-px flex-1 self-center bg-line" />
                      </div>
                    )}
                    <ShotTile
                      shot={shot}
                      index={index}
                      projectApi={projectApi}
                      active={shot.id === openId}
                      checked={checked.has(shot.id)}
                      warnings={warnings.get(shot.id) || []}
                      onOpen={() => setOpenId(shot.id === openId ? null : shot.id)}
                      onCheck={(value) =>
                        setChecked((current) => {
                          const next = new Set(current);
                          if (value) next.add(shot.id);
                          else next.delete(shot.id);
                          return next;
                        })
                      }
                    />
                  </Fragment>
                );
              })}
            </div>
          ) : (
            <div className="min-w-[760px]">
              <div className="sticky top-0 z-10 grid grid-cols-[64px_auto_minmax(0,1.25fr)_minmax(0,1fr)_76px] border-b border-line bg-surface text-[12px] text-ink-3">
                <div className="border-r border-[var(--sheet-guide)] px-3 py-2">镜号</div>
                <div className="border-r border-[var(--sheet-guide)] px-3 py-2" style={{ width: frameWidth(project.format) + 20 }}>
                  画面
                </div>
                <div className="border-r border-[var(--sheet-guide)] px-3.5 py-2">动作与镜头</div>
                <div className="border-r border-[var(--sheet-guide)] px-3.5 py-2">台词</div>
                <div className="px-3 py-2 text-right">秒+帧</div>
              </div>
              {shots.map(({ shot, index }) => (
                <ShotRow
                  key={shot.id}
                  shot={shot}
                  index={index}
                  projectApi={projectApi}
                  active={shot.id === openId}
                  checked={checked.has(shot.id)}
                  warnings={warnings.get(shot.id) || []}
                  onOpen={() => setOpenId(shot.id === openId ? null : shot.id)}
                  onCheck={(value) =>
                    setChecked((current) => {
                      const next = new Set(current);
                      if (value) next.add(shot.id);
                      else next.delete(shot.id);
                      return next;
                    })
                  }
                />
              ))}
            </div>
          )}
          <div className="p-4">
            <Button
              size="sm"
              variant="ghost"
              icon={<Plus className="size-3.5" />}
              onClick={() =>
                projectApi.edit((p) => ({
                  ...p,
                  shots: [...p.shots, { id: newId("shot"), scene: p.shots[p.shots.length - 1]?.scene || "", location_id: p.shots[p.shots.length - 1]?.location_id || null, character_ids: [], size: "中景", camera: "push_in", action: "", image_prompt: "", motion_prompt: "", lines: [], duration: 3, motion: "still", transition: "cut", lipsync: false, face_box: null, seed: null, notes: "" }],
                }))
              }
            >
              添加镜头
            </Button>
          </div>
        </div>
      </div>
      {openShot && (
        <ShotInspector
          key={openShot.id}
          shot={openShot}
          index={project.shots.findIndex((s) => s.id === openShot.id)}
          projectApi={projectApi}
          onClose={() => setOpenId(null)}
          onNavigate={(offset) => {
            const position = project.shots.findIndex((s) => s.id === openShot.id) + offset;
            if (project.shots[position]) setOpenId(project.shots[position].id);
          }}
        />
      )}
    </div>
  );
}
