import { ChevronDown, ImagePlus, Mic, Plus, Sparkles, Trash2, UserRound } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/format";
import type { Character, Location } from "@/lib/types";
import { Button, Menu, MenuContent, MenuItem, MenuTrigger, Select } from "@/components/ui";
import { newId, type ProjectApi } from "./data";
import { characterColor } from "./labels";
import { AudioButton, FrameBox, JobBadge, TakeStrip, UploadButton } from "./media";

function GenerateMenu({ label, onGenerate, disabled }: { label: string; onGenerate: (count: number, framing?: string) => void; disabled?: boolean }) {
  return (
    <Menu>
      <MenuTrigger asChild>
        <Button size="sm" variant="secondary" icon={<Sparkles className="size-3.5" />} disabled={disabled}>
          {label}
          <ChevronDown className="size-3.5 text-ink-3" />
        </Button>
      </MenuTrigger>
      <MenuContent align="start">
        <MenuItem onSelect={() => onGenerate(1)}>生成 1 张</MenuItem>
        <MenuItem onSelect={() => onGenerate(2)}>生成 2 张候选</MenuItem>
        <MenuItem onSelect={() => onGenerate(4)}>生成 4 张候选</MenuItem>
        <MenuItem onSelect={() => onGenerate(2, "full")}>全身立绘 2 张</MenuItem>
      </MenuContent>
    </Menu>
  );
}

function CharacterCard({ character, index, projectApi }: { character: Character; index: number; projectApi: ProjectApi }) {
  const [open, setOpen] = useState(false);
  const refs = projectApi.takes("char_ref", character.id);
  const selected = projectApi.selected("char_ref", character.id);
  const voiceRef = projectApi.selected("voice_ref", character.id);
  const samples = projectApi.takes("voice_sample", character.id);
  const jobs = projectApi.jobsFor("char_ref", character.id);
  const voiceJobs = projectApi.jobsFor("voice_sample", character.id);
  const live = jobs.some((j) => j.state === "running");
  const update = (change: Partial<Character>) =>
    projectApi.edit((p) => ({ ...p, characters: p.characters.map((c) => (c.id === character.id ? { ...c, ...change } : c)) }));
  const lines = projectApi.project!.shots.flatMap((s) => s.lines).filter((l) => l.character_id === character.id).length;

  return (
    <div className="panel flex flex-col gap-4 p-4 sm:flex-row">
      <div className="flex w-full shrink-0 flex-col gap-2 sm:w-[168px]">
        <FrameBox src={selected?.thumb} aspect="3 / 4" guides={false} live={live}>
          {!selected && (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-ink-3">
              <UserRound className="size-8 stroke-[1.3]" />
              <span className="text-[12px]">还没有定妆图</span>
            </div>
          )}
          <div className="absolute top-2 left-2">
            <JobBadge jobs={jobs} />
          </div>
        </FrameBox>
        <TakeStrip
          takes={refs}
          selectedId={selected?.id}
          aspect="3 / 4"
          size={46}
          onSelect={(take) => void projectApi.select("char_ref", character.id, take.id)}
          onDelete={(take) => void projectApi.removeTake(take.id)}
          onRate={(take, rating) => void projectApi.rateTake(take.id, rating)}
        />
        <div className="flex flex-wrap gap-1">
          <GenerateMenu
            label="定妆"
            onGenerate={(count, framing) => void projectApi.generate("char_ref", [character.id], { count, overrides: framing ? { framing } : {} })}
          />
          <UploadButton accept="image/png,image/jpeg,image/webp" onFile={(file) => void projectApi.uploadTo("char_ref", character.id, file)}>
            上传
          </UploadButton>
        </div>
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-3">
        <div className="flex items-center gap-2">
          <span className="size-2.5 shrink-0 rounded-full" style={{ background: character.color || characterColor(index) }} />
          <input value={character.name} onChange={(e) => update({ name: e.target.value })} className="min-w-0 flex-1 bg-transparent text-[17px] font-semibold outline-none" />
          <Select
            size="sm"
            className="w-24"
            value={character.role}
            onChange={(v) => update({ role: v as Character["role"] })}
            options={["主角", "配角", "群演", "旁白"].map((r) => ({ value: r, label: r }))}
          />
          <button
            aria-label="删除角色"
            onClick={() => {
              if (lines && !window.confirm(`${character.name} 有 ${lines} 句台词，删除后这些台词会变成旁白。确定吗？`)) return;
              projectApi.edit((p) => ({
                ...p,
                characters: p.characters.filter((c) => c.id !== character.id),
                shots: p.shots.map((s) => ({
                  ...s,
                  character_ids: s.character_ids.filter((id) => id !== character.id),
                  lines: s.lines.map((l) => (l.character_id === character.id ? { ...l, character_id: null } : l)),
                })),
              }));
            }}
            className="rounded-md p-1.5 text-ink-3 hover:bg-surface-3 hover:text-red"
          >
            <Trash2 className="size-4" />
          </button>
        </div>
        <textarea
          value={character.description}
          onChange={(e) => update({ description: e.target.value })}
          rows={3}
          className="field resize-y text-[13.5px] leading-relaxed"
          placeholder="外貌、发型发色、服装、标志物件、性格"
        />
        <label className="flex flex-col gap-1">
          <span className="text-[12px] text-ink-3">外观提示词（英文，生图时使用）</span>
          <textarea
            value={character.visual_prompt}
            onChange={(e) => update({ visual_prompt: e.target.value })}
            rows={2}
            className="field resize-y font-mono text-[12.5px] leading-relaxed"
            placeholder="young woman, long silver hair, golden eyes, black lace gothic dress"
          />
        </label>
        <div className="rounded-[10px] bg-surface-2 px-3 py-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <Mic className="size-4 text-ink-3" />
            <span className="text-[13px] font-medium">声音</span>
            {voiceRef ? (
              <span className="flex items-center gap-2 text-[12.5px] text-ink-2">
                <AudioButton url={voiceRef.url} label="播放参考音" size="sm" />
                {voiceRef.meta.name || "参考音"}
              </span>
            ) : (
              <span className="text-[12.5px] text-ink-3">还没有参考音，配音会跳过这个角色</span>
            )}
            <div className="ml-auto flex items-center gap-1">
              <UploadButton accept="audio/wav,audio/mpeg,audio/mp4,audio/x-m4a,audio/flac" onFile={(file) => void projectApi.uploadTo("voice_ref", character.id, file)}>
                {voiceRef ? "换参考音" : "上传参考音"}
              </UploadButton>
              <Button size="sm" variant="ghost" disabled={!voiceRef} onClick={() => void projectApi.generate("voice_sample", [character.id])}>
                试音
              </Button>
              <JobBadge jobs={voiceJobs} />
            </div>
          </div>
          <input
            value={character.voice.description}
            onChange={(e) => update({ voice: { ...character.voice, description: e.target.value } })}
            placeholder="声线：年龄感、音色、语速、口头习惯"
            className="mt-2 w-full bg-transparent text-[12.5px] text-ink-2 outline-none placeholder:text-ink-3"
          />
          {samples.length > 0 && (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              {samples.slice(-4).map((sample, i) => (
                <span key={sample.id} className="inline-flex items-center gap-1.5 rounded-full border border-line bg-surface py-0.5 pr-2 pl-0.5 text-[12px]">
                  <AudioButton url={sample.url} size="sm" />
                  试音 {i + 1}
                  <button onClick={() => void projectApi.promoteVoice(sample.id)} className="ml-1 text-blue hover:underline">
                    设为参考音
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>
        <button onClick={() => setOpen(!open)} className="self-start text-[12px] text-ink-3 hover:text-ink">
          {open ? "收起高级设置" : "高级设置：角色 LoRA"}
        </button>
        {open && (
          <div className="flex flex-wrap items-center gap-3">
            <input
              value={character.lora_id || ""}
              onChange={(e) => update({ lora_id: e.target.value || null })}
              placeholder="节点上登记的 LoRA ID"
              className="field h-8 w-56 font-mono text-[12.5px]"
            />
            <span className="text-[12px] text-ink-3">强度</span>
            <input
              type="number"
              min={0}
              max={1.5}
              step={0.05}
              value={character.lora_strength}
              onChange={(e) => update({ lora_strength: Number(e.target.value) })}
              className="field h-8 w-20 text-[12.5px]"
            />
            <span className="text-[12px] text-ink-3">只在单人镜头、Klein 引擎下生效</span>
          </div>
        )}
      </div>
    </div>
  );
}

function LocationCard({ location, projectApi }: { location: Location; projectApi: ProjectApi }) {
  const refs = projectApi.takes("loc_ref", location.id);
  const selected = projectApi.selected("loc_ref", location.id);
  const jobs = projectApi.jobsFor("loc_ref", location.id);
  const aspect = projectApi.project!.format === "16:9" ? "16 / 9" : projectApi.project!.format === "1:1" ? "1 / 1" : "9 / 16";
  const update = (change: Partial<Location>) =>
    projectApi.edit((p) => ({ ...p, locations: p.locations.map((l) => (l.id === location.id ? { ...l, ...change } : l)) }));
  return (
    <div className="panel flex flex-col gap-3 p-4">
      <div className="flex gap-3">
        <div className="w-[120px] shrink-0">
          <FrameBox src={selected?.thumb} aspect={aspect} live={jobs.some((j) => j.state === "running")}>
            <div className="absolute top-1.5 left-1.5">
              <JobBadge jobs={jobs} />
            </div>
          </FrameBox>
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="flex items-center gap-2">
            <input value={location.name} onChange={(e) => update({ name: e.target.value })} className="min-w-0 flex-1 bg-transparent text-[15px] font-semibold outline-none" />
            <button
              aria-label="删除场景"
              onClick={() =>
                projectApi.edit((p) => ({
                  ...p,
                  locations: p.locations.filter((l) => l.id !== location.id),
                  shots: p.shots.map((s) => (s.location_id === location.id ? { ...s, location_id: null } : s)),
                }))
              }
              className="rounded-md p-1 text-ink-3 hover:bg-surface-3 hover:text-red"
            >
              <Trash2 className="size-4" />
            </button>
          </div>
          <textarea value={location.description} onChange={(e) => update({ description: e.target.value })} rows={2} className="field resize-y text-[13px] leading-relaxed" placeholder="空间布局、时间、光线、氛围" />
          <textarea
            value={location.visual_prompt}
            onChange={(e) => update({ visual_prompt: e.target.value })}
            rows={2}
            className="field resize-y font-mono text-[12px] leading-relaxed"
            placeholder="ancient academy courtyard, stone arches, warm afternoon light"
          />
        </div>
      </div>
      <TakeStrip
        takes={refs}
        selectedId={selected?.id}
        aspect={aspect}
        size={44}
        onSelect={(take) => void projectApi.select("loc_ref", location.id, take.id)}
        onDelete={(take) => void projectApi.removeTake(take.id)}
      />
      <div className="flex flex-wrap gap-1">
        <GenerateMenu label="生成场景图" onGenerate={(count) => void projectApi.generate("loc_ref", [location.id], { count })} />
        <UploadButton accept="image/png,image/jpeg,image/webp" onFile={(file) => void projectApi.uploadTo("loc_ref", location.id, file)}>
          上传
        </UploadButton>
      </div>
    </div>
  );
}

export function CastTab({ projectApi }: { projectApi: ProjectApi }) {
  const project = projectApi.project!;
  const styleRef = projectApi.selected("style_ref", project.id);
  const styleTakes = projectApi.takes("style_ref", project.id);
  const missingRefs = project.characters.filter((c) => c.role !== "旁白" && !projectApi.selected("char_ref", c.id));
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-[1180px] flex-col gap-9 px-5 py-6 md:px-8">
        <section>
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <h2 className="text-[17px] font-semibold">角色</h2>
            <span className="text-[13px] text-ink-3">定妆图会作为参考图放进每一镜，保证同一个角色长得一样。</span>
            <div className="ml-auto flex gap-2">
              {missingRefs.length > 1 && (
                <Button size="sm" variant="secondary" icon={<Sparkles className="size-3.5" />} onClick={() => void projectApi.generate("char_ref", missingRefs.map((c) => c.id), { count: 2 })}>
                  给 {missingRefs.length} 个角色定妆
                </Button>
              )}
              <Button
                size="sm"
                variant="ghost"
                icon={<Plus className="size-3.5" />}
                onClick={() =>
                  projectApi.edit((p) => ({
                    ...p,
                    characters: [
                      ...p.characters,
                      { id: newId("char"), name: "新角色", role: "配角", description: "", visual_prompt: "", lora_id: null, lora_strength: 1, voice: { description: "", sample_text: "", speed: 1 }, color: "" },
                    ],
                  }))
                }
              >
                添加角色
              </Button>
            </div>
          </div>
          {project.characters.length ? (
            <div className="grid gap-4 xl:grid-cols-2">
              {project.characters.map((character, index) => (
                <CharacterCard key={character.id} character={character} index={index} projectApi={projectApi} />
              ))}
            </div>
          ) : (
            <div className="rounded-[14px] border border-dashed border-line-strong px-6 py-10 text-center text-[13.5px] text-ink-3">拆分镜时会自动整理出角色；也可以手动添加。</div>
          )}
        </section>
        <section>
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <h2 className="text-[17px] font-semibold">场景</h2>
            <span className="text-[13px] text-ink-3">场景图约束空间布局和光线，让同一地点的镜头接得上。</span>
            <Button
              size="sm"
              variant="ghost"
              className="ml-auto"
              icon={<Plus className="size-3.5" />}
              onClick={() => projectApi.edit((p) => ({ ...p, locations: [...p.locations, { id: newId("loc"), name: "新场景", description: "", visual_prompt: "" }] }))}
            >
              添加场景
            </Button>
          </div>
          {project.locations.length ? (
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {project.locations.map((location) => (
                <LocationCard key={location.id} location={location} projectApi={projectApi} />
              ))}
            </div>
          ) : (
            <div className="rounded-[14px] border border-dashed border-line-strong px-6 py-8 text-center text-[13.5px] text-ink-3">还没有场景</div>
          )}
        </section>
        <section className="pb-10">
          <h2 className="mb-1 text-[17px] font-semibold">画风参考</h2>
          <p className="mb-3 text-[13px] text-ink-3">上传一张你认可的成品图，生成时只参考它的渲染、配色和光线，不参考里面的人物。</p>
          <div className="flex flex-wrap items-end gap-4">
            <div className={cn("w-[150px]")}>
              <FrameBox src={styleRef?.thumb} aspect="3 / 4" guides={false}>
                {!styleRef && (
                  <div className="absolute inset-0 flex items-center justify-center text-ink-3">
                    <ImagePlus className="size-7 stroke-[1.3]" />
                  </div>
                )}
              </FrameBox>
            </div>
            <div className="flex flex-col gap-2">
              <TakeStrip
                takes={styleTakes}
                selectedId={styleRef?.id}
                aspect="3 / 4"
                size={40}
                onSelect={(take) => void projectApi.select("style_ref", project.id, take.id)}
                onDelete={(take) => void projectApi.removeTake(take.id)}
              />
              <div className="flex gap-1">
                <UploadButton accept="image/png,image/jpeg,image/webp" onFile={(file) => void projectApi.uploadTo("style_ref", project.id, file)}>
                  上传画风参考
                </UploadButton>
                {styleRef && (
                  <Button size="sm" variant="ghost" onClick={() => void projectApi.select("style_ref", project.id, null)}>
                    不使用
                  </Button>
                )}
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
