import { Download, Film, Music, Play, Trash2 } from "lucide-react";
import { useMemo } from "react";
import { cn, duration, relativeTime, timecode } from "@/lib/format";
import type { MusicCue, Take } from "@/lib/types";
import { Button, Empty, ProgressBar, Slider, Spinner, Switch } from "@/components/ui";
import { newId, type ProjectApi } from "./data";
import { FORMAT_ASPECT } from "./labels";
import { AudioButton, UploadButton } from "./media";

/** Player width that keeps the whole frame, the controls and the timeline on one screen. */
function stageWidth(format: string): string {
  const ratio = format === "9:16" ? 9 / 16 : format === "1:1" ? 1 : 16 / 9;
  const cap = format === "9:16" ? "380px" : format === "1:1" ? "560px" : "100%";
  return `min(${cap}, calc((100dvh - 360px) * ${ratio.toFixed(4)}))`;
}

function Timeline({ projectApi }: { projectApi: ProjectApi }) {
  const project = projectApi.project!;
  const total = Math.max(1, project.shots.reduce((sum, s) => sum + s.duration, 0));
  let cursor = 0;
  return (
    <div>
      <div className="flex h-16 overflow-hidden rounded-[8px] border border-line bg-surface-2">
        {project.shots.map((shot, index) => {
          const frame = projectApi.selected("frame", shot.id);
          const clip = projectApi.selected("clip", shot.id);
          const voiced = shot.lines.filter((l) => projectApi.selected("voice", l.id)).length;
          const width = (shot.duration / total) * 100;
          const start = cursor;
          cursor += shot.duration;
          return (
            <div
              key={shot.id}
              title={`C${index + 1}　${timecode(start)}　${shot.action}`}
              style={{ width: `${width}%` }}
              className={cn("relative h-full shrink-0 border-r border-surface", !frame && "bg-[repeating-linear-gradient(45deg,var(--surface-3)_0_6px,var(--surface-2)_6px_12px)]")}
            >
              {frame?.thumb && <img src={clip?.thumb || frame.thumb} alt="" className="absolute inset-0 size-full object-cover" />}
              <span className="tabular absolute top-0.5 left-1 text-[10px] font-semibold text-white mix-blend-difference">{index + 1}</span>
              {shot.lines.length > 0 && (
                <span className={cn("absolute right-0 bottom-0 left-0 h-1", voiced === shot.lines.length ? "bg-green" : voiced ? "bg-accent" : "bg-red/70")} />
              )}
              {clip && <span className="absolute top-0.5 right-1 size-1.5 rounded-full bg-green" />}
            </div>
          );
        })}
      </div>
      <div className="tabular mt-1.5 flex justify-between text-[11.5px] text-ink-3">
        <span>00:00</span>
        <span className="flex gap-3">
          <span className="inline-flex items-center gap-1">
            <span className="h-1 w-3 rounded bg-green" />
            配音齐
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="h-1 w-3 rounded bg-red/70" />
            缺配音
          </span>
        </span>
        <span>{timecode(total)}</span>
      </div>
    </div>
  );
}

function RenderRow({ take, active, onPlay }: { take: Take; active: boolean; onPlay: () => void }) {
  const meta = take.meta;
  return (
    <div className={cn("flex items-center gap-3 rounded-[10px] px-3 py-2.5", active ? "bg-accent-soft" : "hover:bg-surface-2")}>
      <button onClick={onPlay} className="relative h-12 w-12 shrink-0 overflow-hidden rounded-[6px] bg-surface-3" aria-label="播放">
        {take.thumb && <img src={take.thumb} alt="" className="absolute inset-0 size-full object-cover" />}
        <Play className="absolute inset-0 m-auto size-4 text-white drop-shadow" />
      </button>
      <div className="min-w-0 flex-1">
        <div className="text-[13.5px] font-medium">
          {meta.quality === "final" ? "1080p 成片" : "预览"}　{duration(Number(meta.duration) || 0)}
        </div>
        <div className="text-[12px] text-ink-3">
          {relativeTime(take.created_at)}　{meta.shots} 镜
          {meta.missing_frames > 0 && <span className="text-accent-ink">　{meta.missing_frames} 镜缺画面</span>}
        </div>
      </div>
      <a href={take.url} download className="rounded-md p-2 text-ink-3 hover:bg-surface-3 hover:text-ink" aria-label="下载视频">
        <Download className="size-4" />
      </a>
      {take.srt_url && (
        <a href={take.srt_url} download className="rounded-md px-2 py-1 text-[12px] text-ink-3 hover:bg-surface-3 hover:text-ink">
          字幕
        </a>
      )}
    </div>
  );
}

export function FilmTab({ projectApi }: { projectApi: ProjectApi }) {
  const project = projectApi.project!;
  const view = projectApi.view!;
  const renders = useMemo(() => projectApi.takes("render", project.id).slice().reverse(), [projectApi, project.id]);
  const current = projectApi.selected("render", project.id) || renders[0];
  const renderJob = view.jobs.find((j) => j.kind === "render" && (j.state === "running" || j.state === "queued"));
  const lastRender = view.jobs.find((j) => j.kind === "render");
  const musicTakes = projectApi.takes("music", project.id);
  const p = view.progress;
  const aspect = FORMAT_ASPECT[project.format];

  const addMusic = (take: Take) =>
    projectApi.edit((proj) => ({
      ...proj,
      music: [...proj.music, { id: newId("music"), asset_id: take.asset_id, name: take.meta.name || "背景音乐", gain: 0.35, start: 0, duck: true }],
    }));
  const updateCue = (id: string, change: Partial<MusicCue>) => projectApi.edit((proj) => ({ ...proj, music: proj.music.map((m) => (m.id === id ? { ...m, ...change } : m)) }));

  if (!project.shots.length) {
    return <Empty icon={<Film />} title="还没有可以合成的镜头">先写剧本、拆分镜，生成画面和配音后在这里合成成片。</Empty>;
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto grid max-w-[1180px] gap-8 px-5 py-6 md:px-8 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="flex min-w-0 flex-col gap-5">
          {/* Screening room: the film always plays on a dark stage, whatever the theme. */}
          <div className="flex justify-center rounded-[16px] bg-[#0e1014] p-3 sm:p-5">
            <div className="w-full overflow-hidden rounded-[8px] bg-black shadow-[0_24px_60px_-24px_rgb(0_0_0/0.9)]" style={{ maxWidth: stageWidth(project.format) }}>
              {current ? (
                <video key={current.id} src={current.url} poster={current.poster_url} controls playsInline className="block w-full" style={{ aspectRatio: aspect }} />
              ) : (
                <div className="flex flex-col items-center justify-center gap-2 text-[13px] text-white/55" style={{ aspectRatio: aspect }}>
                  <Film className="size-8 stroke-[1.3]" />
                  还没有合成过
                </div>
              )}
            </div>
          </div>
          {renderJob ? (
            <div className="rounded-[12px] bg-accent-soft px-4 py-3">
              <div className="mb-2 flex items-center gap-2 text-[13px] font-medium text-accent-ink">
                <Spinner className="size-3.5 text-accent-ink" />
                {renderJob.state === "queued" ? "合成排队中" : renderJob.progress.stage || "合成中"}
              </div>
              <ProgressBar value={renderJob.progress.fraction || 0.02} />
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              <Button onClick={() => void projectApi.render("preview")} icon={<Play className="size-4" />}>
                合成预览
              </Button>
              <Button variant="primary" onClick={() => void projectApi.render("final")} icon={<Film className="size-4" />}>
                导出 1080p 成片
              </Button>
              <span className="text-[12.5px] text-ink-3">预览 540p 很快；成片 1080p/30 帧，带字幕和混音。</span>
            </div>
          )}
          {lastRender?.state === "failed" && <div className="rounded-[10px] bg-red-soft px-3.5 py-2.5 text-[13px] text-red">{lastRender.error}</div>}
          <div>
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-[14px] font-semibold">时间线</h3>
              <span className="text-[12.5px] text-ink-3">
                画面 {p.frames}/{p.shots}　配音 {p.voices}/{p.lines}　动态 {p.clips}/{p.moving}
              </span>
            </div>
            <Timeline projectApi={projectApi} />
          </div>
          {renders.length > 0 && (
            <div>
              <h3 className="mb-2 text-[14px] font-semibold">合成记录</h3>
              <div className="flex flex-col gap-1">
                {renders.map((take) => (
                  <RenderRow key={take.id} take={take} active={take.id === current?.id} onPlay={() => void projectApi.select("render", project.id, take.id)} />
                ))}
              </div>
            </div>
          )}
        </div>
        <aside className="flex flex-col gap-6">
          <section>
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-[14px] font-semibold">背景音乐</h3>
              <UploadButton accept="audio/wav,audio/mpeg,audio/mp4,audio/x-m4a,audio/flac" onFile={(file) => void projectApi.uploadTo("music", project.id, file).then((take) => take && addMusic(take))}>
                添加音乐
              </UploadButton>
            </div>
            {project.music.length === 0 && <p className="text-[12.5px] leading-relaxed text-ink-3">上传一段配乐，会自动循环到片尾，台词出现时自动压低音量。</p>}
            <div className="flex flex-col gap-2">
              {project.music.map((cue) => {
                const take = musicTakes.find((t) => t.asset_id === cue.asset_id);
                return (
                  <div key={cue.id} className="rounded-[12px] border border-line p-3">
                    <div className="flex items-center gap-2">
                      <AudioButton url={take?.url} size="sm" />
                      <Music className="size-3.5 text-ink-3" />
                      <span className="min-w-0 flex-1 truncate text-[13px]">{cue.name}</span>
                      <button aria-label="移除音乐" onClick={() => projectApi.edit((proj) => ({ ...proj, music: proj.music.filter((m) => m.id !== cue.id) }))} className="rounded p-1 text-ink-3 hover:text-red">
                        <Trash2 className="size-3.5" />
                      </button>
                    </div>
                    <div className="mt-3 flex items-center gap-3">
                      <span className="w-10 text-[12px] text-ink-3">音量</span>
                      <Slider value={cue.gain} min={0} max={1} step={0.05} onChange={(v) => updateCue(cue.id, { gain: v })} label="音乐音量" />
                    </div>
                    <label className="mt-2 flex items-center justify-between text-[12.5px] text-ink-2">
                      台词时自动压低
                      <Switch checked={cue.duck} onChange={(v) => updateCue(cue.id, { duck: v })} />
                    </label>
                  </div>
                );
              })}
            </div>
          </section>
          <section>
            <h3 className="mb-3 text-[14px] font-semibold">字幕</h3>
            <div className="flex flex-col gap-3">
              <label className="flex items-center justify-between text-[13px]">
                烧录字幕
                <Switch checked={project.subtitle.enabled} onChange={(v) => projectApi.edit((proj) => ({ ...proj, subtitle: { ...proj.subtitle, enabled: v } }))} />
              </label>
              <label className="flex items-center justify-between text-[13px]">
                显示说话人
                <Switch checked={project.subtitle.speaker} onChange={(v) => projectApi.edit((proj) => ({ ...proj, subtitle: { ...proj.subtitle, speaker: v } }))} />
              </label>
              <div className="flex items-center gap-3">
                <span className="w-10 text-[12px] text-ink-3">字号</span>
                <Slider value={project.subtitle.size} min={0.025} max={0.07} step={0.002} onChange={(v) => projectApi.edit((proj) => ({ ...proj, subtitle: { ...proj.subtitle, size: v } }))} label="字幕字号" />
              </div>
              <div className="flex items-center gap-3">
                <span className="w-10 text-[12px] text-ink-3">位置</span>
                <Slider value={project.subtitle.position} min={0.6} max={0.93} step={0.01} onChange={(v) => projectApi.edit((proj) => ({ ...proj, subtitle: { ...proj.subtitle, position: v } }))} label="字幕位置" />
              </div>
              <p className="text-[12px] leading-relaxed text-ink-3">竖屏建议放在 75%–82% 高度，避开平台底部的按钮和文案。</p>
            </div>
          </section>
          <section>
            <h3 className="mb-2 text-[14px] font-semibold">补帧</h3>
            <label className="flex items-center justify-between gap-3 text-[13px]">
              <span>
                动作补帧（实验）
                <span className="block text-[12px] text-ink-3">16 帧动作插值到 30 帧，动作快时可能扭曲</span>
              </span>
              <Switch checked={project.smooth_motion} onChange={(v) => projectApi.edit((proj) => ({ ...proj, smooth_motion: v }))} />
            </label>
          </section>
        </aside>
      </div>
    </div>
  );
}
