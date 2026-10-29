import { ChevronDown, ChevronLeft, ChevronRight, Copy, Film, Mic, Plus, Sparkles, Trash2, Wand2, X } from "lucide-react";
import { useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { cn } from "@/lib/format";
import type { Line, Shot } from "@/lib/types";
import { useIsPhone } from "@/hooks/useMedia";
import { Button, IconButton, Menu, MenuContent, MenuItem, MenuTrigger, Segmented, Select, Slider, Switch } from "@/components/ui";
import { newId, useOverview, type ProjectApi } from "./data";
import { CAMERAS, characterColor, FORMAT_ASPECT, MOTIONS, SIZES, TRANSITIONS } from "./labels";
import { AudioButton, FrameBox, JobBadge, TakeStrip, UploadButton, VideoTake } from "./media";

function Section({ title, children, aside }: { title: ReactNode; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="border-b border-line px-5 py-4">
      <div className="mb-3 flex items-center gap-2">
        <h3 className="text-[13.5px] font-semibold">{title}</h3>
        <div className="ml-auto flex items-center gap-1">{aside}</div>
      </div>
      {children}
    </section>
  );
}

function CountMenu({ label, icon, onGenerate, disabled }: { label: string; icon: ReactNode; onGenerate: (count: number) => void; disabled?: boolean }) {
  return (
    <Menu>
      <MenuTrigger asChild>
        <Button size="sm" icon={icon} disabled={disabled}>
          {label}
          <ChevronDown className="size-3.5 text-ink-3" />
        </Button>
      </MenuTrigger>
      <MenuContent>
        <MenuItem onSelect={() => onGenerate(1)}>生成 1 个</MenuItem>
        <MenuItem onSelect={() => onGenerate(2)}>生成 2 个候选</MenuItem>
        <MenuItem onSelect={() => onGenerate(4)}>生成 4 个候选</MenuItem>
      </MenuContent>
    </Menu>
  );
}

/** Drag a rectangle on the frame to mark the speaking face for lip sync. */
function FaceBoxPicker({ src, aspect, value, onChange }: { src?: string; aspect: string; value: number[] | null; onChange: (box: number[]) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState<number[] | null>(null);
  const start = useRef<[number, number] | null>(null);
  const point = (event: ReactPointerEvent) => {
    const rect = ref.current!.getBoundingClientRect();
    return [Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)), Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height))] as [number, number];
  };
  const box = draft || value;
  return (
    <div
      ref={ref}
      onPointerDown={(e) => {
        (e.target as HTMLElement).setPointerCapture(e.pointerId);
        start.current = point(e);
        setDraft([...start.current, ...start.current]);
      }}
      onPointerMove={(e) => {
        if (!start.current) return;
        const [x, y] = point(e);
        const [sx, sy] = start.current;
        setDraft([Math.min(sx, x), Math.min(sy, y), Math.max(sx, x), Math.max(sy, y)]);
      }}
      onPointerUp={() => {
        if (draft && draft[2] - draft[0] > 0.03 && draft[3] - draft[1] > 0.03) onChange(draft.map((n) => Math.round(n * 1000) / 1000));
        start.current = null;
        setDraft(null);
      }}
      className="relative cursor-crosshair touch-none select-none"
    >
      <FrameBox src={src} aspect={aspect} guides={false} />
      {box && (
        <div
          className="absolute rounded-[3px] border-2 border-accent bg-accent/15"
          style={{ left: `${box[0] * 100}%`, top: `${box[1] * 100}%`, width: `${(box[2] - box[0]) * 100}%`, height: `${(box[3] - box[1]) * 100}%` }}
        />
      )}
    </div>
  );
}

function LineEditor({ line, shot, projectApi }: { line: Line; shot: Shot; projectApi: ProjectApi }) {
  const project = projectApi.project!;
  const overview = useOverview();
  const voices = projectApi.takes("voice", line.id);
  const selected = projectApi.selected("voice", line.id);
  const jobs = projectApi.jobsFor("voice", line.id);
  const speaker = project.characters.find((c) => c.id === line.character_id);
  const hasRef = speaker ? Boolean(projectApi.selected("voice_ref", speaker.id)) : project.characters.some((c) => c.role === "旁白" && projectApi.selected("voice_ref", c.id));
  const update = (change: Partial<Line>) =>
    projectApi.edit((p) => ({
      ...p,
      shots: p.shots.map((s) => (s.id === shot.id ? { ...s, lines: s.lines.map((l) => (l.id === line.id ? { ...l, ...change } : l)) } : s)),
    }));
  return (
    <div className="rounded-[12px] border border-line p-3">
      <div className="flex items-center gap-2">
        <Select
          size="sm"
          className="w-32"
          value={line.character_id || "narrator"}
          onChange={(v) => update({ character_id: v === "narrator" ? null : v })}
          options={[
            ...project.characters.map((c, i) => ({ value: c.id, label: <span style={{ color: c.color || characterColor(i) }}>{c.name}</span> })),
            { value: "narrator", label: "旁白" },
          ]}
        />
        <Select size="sm" className="w-24" value={line.emotion} onChange={(v) => update({ emotion: v })} options={(overview.data?.emotions || ["平静"]).map((e) => ({ value: e, label: e }))} />
        <div className="ml-auto flex items-center gap-1">
          <JobBadge jobs={jobs} />
          <IconButton
            label="删除台词"
            size="sm"
            onClick={() => projectApi.edit((p) => ({ ...p, shots: p.shots.map((s) => (s.id === shot.id ? { ...s, lines: s.lines.filter((l) => l.id !== line.id) } : s)) }))}
          >
            <Trash2 className="size-3.5" />
          </IconButton>
        </div>
      </div>
      <textarea value={line.text} onChange={(e) => update({ text: e.target.value })} rows={2} className="field mt-2 resize-y text-[14px] leading-relaxed" />
      {line.emotion !== "平静" && (
        <div className="mt-2 flex items-center gap-3">
          <span className="w-16 text-[12px] text-ink-3">情绪强度</span>
          <Slider value={line.intensity} min={0.2} max={1} step={0.05} onChange={(v) => update({ intensity: v })} label="情绪强度" />
          <span className="tabular w-8 text-right text-[12px] text-ink-3">{Math.round(line.intensity * 100)}</span>
        </div>
      )}
      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        {voices.map((take, i) => (
          <span
            key={take.id}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full border py-0.5 pr-2 pl-0.5 text-[12px]",
              take.id === selected?.id ? "border-accent bg-accent-soft text-accent-ink" : "border-line",
            )}
          >
            <AudioButton url={take.url} size="sm" />
            <button onClick={() => void projectApi.select("voice", line.id, take.id)}>
              第 {i + 1} 条{take.meta.duration ? ` ${Number(take.meta.duration).toFixed(1)}s` : ""}
            </button>
          </span>
        ))}
        <Button size="sm" variant="ghost" icon={<Mic className="size-3.5" />} disabled={!hasRef || !line.text.trim()} onClick={() => void projectApi.generate("voice", [line.id])}>
          {voices.length ? "再来一条" : "配音"}
        </Button>
        {selected && speaker && (
          <button onClick={() => void projectApi.promoteVoice(selected.id)} className="text-[12px] text-blue hover:underline">
            设为 {speaker.name} 的参考音
          </button>
        )}
        {!hasRef && <span className="text-[12px] text-ink-3">先在角色页给 {speaker?.name || "旁白"} 上传参考音</span>}
      </div>
    </div>
  );
}

export function ShotInspector({
  shot,
  index,
  projectApi,
  onClose,
  onNavigate,
}: {
  shot: Shot;
  index: number;
  projectApi: ProjectApi;
  onClose: () => void;
  onNavigate: (offset: number) => void;
}) {
  const phone = useIsPhone();
  const overview = useOverview();
  const project = projectApi.project!;
  const aspect = FORMAT_ASPECT[project.format];
  const frames = projectApi.takes("frame", shot.id);
  const frame = projectApi.selected("frame", shot.id);
  const endFrames = projectApi.takes("end_frame", shot.id);
  const endFrame = projectApi.selected("end_frame", shot.id);
  const clips = projectApi.takes("clip", shot.id);
  const clip = projectApi.selected("clip", shot.id);
  const lipsyncs = projectApi.takes("lipsync", shot.id);
  const lipsync = projectApi.selected("lipsync", shot.id);
  const [showPrompt, setShowPrompt] = useState(false);
  const engines = overview.data?.engines || [];
  const update = (change: Partial<Shot>) => projectApi.edit((p) => ({ ...p, shots: p.shots.map((s) => (s.id === shot.id ? { ...s, ...change } : s)) }));
  const videoEngines = engines.filter((e) => e.kind === "video" && (shot.motion === "flf2v" ? e.end_frame : !e.end_frame));
  const [videoEngine, setVideoEngine] = useState(shot.motion === "flf2v" ? "wan22-flf-fast" : project.engines.video);
  const imageEngines = engines.filter((e) => e.kind === "image");
  const [imageEngine, setImageEngine] = useState(project.engines.image);

  return (
    <aside className={cn("flex flex-col bg-surface", phone ? "fixed inset-0 z-30" : "w-[460px] shrink-0")}>
      <div className="flex h-12 shrink-0 items-center gap-1 border-b border-line px-3">
        <IconButton label="上一镜" onClick={() => onNavigate(-1)} disabled={index === 0}>
          <ChevronLeft className="size-4" />
        </IconButton>
        <span className="tabular text-[15px] font-semibold">C{String(index + 1).padStart(2, "0")}</span>
        <IconButton label="下一镜" onClick={() => onNavigate(1)} disabled={index === project.shots.length - 1}>
          <ChevronRight className="size-4" />
        </IconButton>
        <span className="ml-1 truncate text-[12.5px] text-ink-3">{shot.scene}</span>
        <IconButton label="关闭" className="ml-auto" onClick={onClose}>
          <X className="size-4" />
        </IconButton>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <Section
          title="画面"
          aside={
            <>
              <JobBadge jobs={projectApi.jobsFor("frame", shot.id)} />
              <UploadButton accept="image/png,image/jpeg,image/webp" onFile={(file) => void projectApi.uploadTo("frame", shot.id, file)} />
            </>
          }
        >
          <div className={cn("mx-auto", project.format === "9:16" ? "w-[220px]" : "w-full")}>
            <FrameBox src={frame?.url} aspect={aspect} live={projectApi.jobsFor("frame", shot.id).some((j) => j.state === "running")}>
              {!frame && <div className="absolute inset-0 flex items-center justify-center text-[12.5px] text-ink-3">还没有画面</div>}
            </FrameBox>
          </div>
          <div className="mt-3">
            <TakeStrip
              takes={frames}
              selectedId={frame?.id}
              aspect={aspect}
              size={project.format === "16:9" ? 92 : 52}
              onSelect={(take) => void projectApi.select("frame", shot.id, take.id)}
              onDelete={(take) => void projectApi.removeTake(take.id)}
              onRate={(take, rating) => void projectApi.rateTake(take.id, rating)}
            />
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <CountMenu label="生成画面" icon={<Sparkles className="size-3.5" />} onGenerate={(count) => void projectApi.generate("frame", [shot.id], { count, engine: imageEngine })} />
            <Select size="sm" className="w-[190px]" value={imageEngine} onChange={setImageEngine} options={imageEngines.map((e) => ({ value: e.id, label: e.label, hint: e.available ? undefined : e.reason }))} />
          </div>
          <label className="mt-4 flex flex-col gap-1">
            <span className="text-[12px] text-ink-3">画面描述（这一刻画面里发生了什么）</span>
            <textarea value={shot.action} onChange={(e) => update({ action: e.target.value })} rows={3} className="field resize-y text-[13.5px] leading-relaxed" />
          </label>
          <div className="mt-3 flex flex-wrap gap-2">
            {project.characters.map((c, i) => {
              const on = shot.character_ids.includes(c.id);
              return (
                <button
                  key={c.id}
                  onClick={() => update({ character_ids: on ? shot.character_ids.filter((id) => id !== c.id) : [...shot.character_ids, c.id] })}
                  className={cn("inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-[12.5px]", on ? "border-line-strong bg-surface-3" : "border-line text-ink-3")}
                >
                  <span className="size-2 rounded-full" style={{ background: on ? c.color || characterColor(i) : "var(--line-strong)" }} />
                  {c.name}
                </button>
              );
            })}
          </div>
          <div className="mt-3 grid grid-cols-2 gap-2">
            <Select size="sm" value={shot.location_id || "none"} onChange={(v) => update({ location_id: v === "none" ? null : v })} options={[{ value: "none", label: "不指定场景" }, ...project.locations.map((l) => ({ value: l.id, label: l.name }))]} />
            <Select size="sm" value={shot.size} onChange={(v) => update({ size: v as Shot["size"] })} options={SIZES.map((s) => ({ value: s, label: s }))} />
          </div>
          <button onClick={() => setShowPrompt(!showPrompt)} className="mt-3 text-[12px] text-ink-3 hover:text-ink">
            {showPrompt ? "收起英文提示词" : "查看英文提示词"}
          </button>
          {showPrompt && (
            <div className="mt-2 flex flex-col gap-2">
              <textarea value={shot.image_prompt} onChange={(e) => update({ image_prompt: e.target.value })} rows={4} className="field resize-y font-mono text-[12px] leading-relaxed" />
              <Button size="sm" variant="ghost" icon={<Wand2 className="size-3.5" />} className="self-start" onClick={() => void projectApi.director("prompts", { shot_ids: [shot.id] })}>
                按画面描述和台词重写提示词
              </Button>
            </div>
          )}
        </Section>

        <Section title="动态" aside={<JobBadge jobs={[...projectApi.jobsFor("clip", shot.id), ...projectApi.jobsFor("end_frame", shot.id)]} />}>
          <Segmented
            size="sm"
            value={shot.motion}
            onChange={(v) => {
              update({ motion: v });
              setVideoEngine(v === "flf2v" ? "wan22-flf-fast" : project.engines.video);
            }}
            options={MOTIONS.map((m) => ({ value: m.value, label: m.label }))}
          />
          <p className="mt-2 text-[12px] text-ink-3">{MOTIONS.find((m) => m.value === shot.motion)?.hint}</p>
          {shot.motion === "still" ? (
            <div className="mt-3 grid grid-cols-2 gap-2">
              <Select size="sm" value={shot.camera} onChange={(v) => update({ camera: v as Shot["camera"] })} options={CAMERAS.map((c) => ({ value: c.value, label: `运镜：${c.label}` }))} />
              <Select size="sm" value={shot.transition} onChange={(v) => update({ transition: v as Shot["transition"] })} options={TRANSITIONS.map((t) => ({ value: t.value, label: `转场：${t.label}` }))} />
            </div>
          ) : (
            <div className="mt-3 flex flex-col gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-[12px] text-ink-3">动作提示词（英文，只写这几秒的运动）</span>
                <textarea value={shot.motion_prompt} onChange={(e) => update({ motion_prompt: e.target.value })} rows={3} className="field resize-y font-mono text-[12px] leading-relaxed" placeholder="she slowly raises her right hand, golden light gathers in her palm" />
              </label>
              {shot.motion === "flf2v" && (
                <div>
                  <div className="mb-2 text-[12px] text-ink-3">结束画面</div>
                  <TakeStrip takes={endFrames} selectedId={endFrame?.id} aspect={aspect} size={52} onSelect={(take) => void projectApi.select("end_frame", shot.id, take.id)} onDelete={(take) => void projectApi.removeTake(take.id)} />
                  <div className="mt-2 flex gap-1">
                    <CountMenu label="生成结束画面" icon={<Sparkles className="size-3.5" />} disabled={!frame} onGenerate={(count) => void projectApi.generate("end_frame", [shot.id], { count, engine: imageEngine })} />
                    <UploadButton accept="image/png,image/jpeg,image/webp" onFile={(file) => void projectApi.uploadTo("end_frame", shot.id, file)} />
                  </div>
                </div>
              )}
              {clip && (
                <div className={cn("mx-auto", project.format === "9:16" ? "w-[220px]" : "w-full")}>
                  <VideoTake take={clip} aspect={aspect} />
                </div>
              )}
              <TakeStrip takes={clips} selectedId={clip?.id} aspect={aspect} size={52} onSelect={(take) => void projectApi.select("clip", shot.id, take.id)} onDelete={(take) => void projectApi.removeTake(take.id)} />
              <div className="flex flex-wrap items-center gap-2">
                <CountMenu label="生成动态" icon={<Film className="size-3.5" />} disabled={!frame || (shot.motion === "flf2v" && !endFrame)} onGenerate={(count) => void projectApi.generate("clip", [shot.id], { count, engine: videoEngine })} />
                <Select size="sm" className="w-[190px]" value={videoEngine} onChange={setVideoEngine} options={videoEngines.map((e) => ({ value: e.id, label: e.label, hint: e.available ? undefined : e.reason }))} />
              </div>
              {!frame && <p className="text-[12px] text-ink-3">先选定这一镜的画面，再生成动态。</p>}
              <Select size="sm" value={shot.transition} onChange={(v) => update({ transition: v as Shot["transition"] })} options={TRANSITIONS.map((t) => ({ value: t.value, label: `转场：${t.label}` }))} />
            </div>
          )}
          <div className="mt-4 flex items-center gap-3">
            <span className="w-14 text-[12px] text-ink-3">时长</span>
            <Slider value={shot.duration} min={1} max={10} step={0.1} onChange={(v) => update({ duration: Math.round(v * 10) / 10 })} label="镜头时长" />
            <span className="tabular w-12 text-right text-[13px]">{shot.duration.toFixed(1)}s</span>
          </div>
          <p className="mt-1 text-[11.5px] text-ink-3">台词比镜头长时，合成会自动延长这一镜。</p>
        </Section>

        <Section
          title="台词"
          aside={
            <Button
              size="sm"
              variant="ghost"
              icon={<Plus className="size-3.5" />}
              onClick={() =>
                update({ lines: [...shot.lines, { id: newId("line"), character_id: shot.character_ids[0] || null, text: "", emotion: "平静", intensity: 0.6, pause_after: 0.25 }] })
              }
            >
              添加台词
            </Button>
          }
        >
          <div className="flex flex-col gap-2.5">
            {shot.lines.map((line) => (
              <LineEditor key={line.id} line={line} shot={shot} projectApi={projectApi} />
            ))}
            {shot.lines.length === 0 && <p className="text-[12.5px] text-ink-3">这一镜没有台词，合成时只放画面和音乐。</p>}
          </div>
        </Section>

        {shot.motion !== "still" && (
          <Section title="口型" aside={<Switch checked={shot.lipsync} onChange={(v) => update({ lipsync: v })} label="对口型" />}>
            {shot.lipsync ? (
              <div className="flex flex-col gap-3">
                <p className="text-[12px] leading-relaxed text-ink-3">在画面上框出说话人的脸（只动嘴部，正脸近景效果最好；生成很慢，每秒约 5 分钟）。</p>
                <div className={cn(project.format === "9:16" ? "w-[200px]" : "w-full")}>
                  <FaceBoxPicker src={frame?.url} aspect={aspect} value={shot.face_box} onChange={(box) => update({ face_box: box })} />
                </div>
                {lipsync && (
                  <div className={cn(project.format === "9:16" ? "w-[220px]" : "w-full")}>
                    <VideoTake take={lipsync} aspect={aspect} />
                  </div>
                )}
                <TakeStrip takes={lipsyncs} selectedId={lipsync?.id} aspect={aspect} size={52} onSelect={(take) => void projectApi.select("lipsync", shot.id, take.id)} onDelete={(take) => void projectApi.removeTake(take.id)} />
                <Button size="sm" icon={<Mic className="size-3.5" />} disabled={!clip || !shot.face_box} onClick={() => void projectApi.generate("lipsync", [shot.id])} className="self-start">
                  生成口型
                </Button>
              </div>
            ) : (
              <p className="text-[12.5px] text-ink-3">需要时打开：给正脸说话的动态镜头对上口型。</p>
            )}
          </Section>
        )}

        <div className="flex flex-wrap gap-2 px-5 py-4">
          <Button
            size="sm"
            variant="ghost"
            icon={<Copy className="size-3.5" />}
            onClick={() =>
              projectApi.edit((p) => {
                const shots = [...p.shots];
                const at = shots.findIndex((s) => s.id === shot.id);
                shots.splice(at + 1, 0, { ...shot, id: newId("shot"), lines: shot.lines.map((l) => ({ ...l, id: newId("line") })) });
                return { ...p, shots };
              })
            }
          >
            复制一镜
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              projectApi.edit((p) => {
                const shots = [...p.shots];
                const at = shots.findIndex((s) => s.id === shot.id);
                if (at > 0) [shots[at - 1], shots[at]] = [shots[at], shots[at - 1]];
                return { ...p, shots };
              })
            }
          >
            上移
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              projectApi.edit((p) => {
                const shots = [...p.shots];
                const at = shots.findIndex((s) => s.id === shot.id);
                if (at < shots.length - 1) [shots[at + 1], shots[at]] = [shots[at], shots[at + 1]];
                return { ...p, shots };
              })
            }
          >
            下移
          </Button>
          <Button
            size="sm"
            variant="danger"
            className="ml-auto"
            icon={<Trash2 className="size-3.5" />}
            onClick={() => {
              if (!window.confirm(`删除第 ${index + 1} 镜？`)) return;
              onClose();
              projectApi.edit((p) => ({ ...p, shots: p.shots.filter((s) => s.id !== shot.id) }));
            }}
          >
            删除这一镜
          </Button>
        </div>
      </div>
    </aside>
  );
}
