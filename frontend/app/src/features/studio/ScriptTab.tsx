import { Clapperboard, PenLine, Wand2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { cn, duration, modelLabel } from "@/lib/format";
import { Button, Field, ProgressBar, Spinner } from "@/components/ui";
import { CloudBadge, useModels } from "@/features/chat/ModelPicker";
import type { ProjectApi } from "./data";
import { ScriptEditor, ScriptHighlight } from "./ScriptEditor";

/** Rough reading time: dialogue at ~4.5 characters per second plus shot breathing room. */
function estimateSeconds(script: string): number {
  const dialogue = script
    .split("\n")
    .filter((line) => /[：:]/.test(line) && !line.startsWith("【") && !line.startsWith("#"))
    .map((line) => line.split(/[：:]/).slice(1).join(":"))
    .join("");
  const actions = script.split("\n").filter((line) => line.startsWith("【")).length;
  return dialogue.length / 4.5 + actions * 2.5;
}

export function ScriptTab({ projectApi }: { projectApi: ProjectApi }) {
  const navigate = useNavigate();
  const project = projectApi.project!;
  const jobs = projectApi.view?.jobs || [];
  const writing = jobs.find((j) => j.kind === "director:script" && (j.state === "running" || j.state === "queued"));
  const breaking = jobs.find((j) => j.kind === "director:breakdown" && (j.state === "running" || j.state === "queued"));
  const lastBreak = jobs.find((j) => j.kind === "director:breakdown");
  const [instructions, setInstructions] = useState("");
  const liveText = writing?.progress?.text || "";
  const liveRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    liveRef.current?.scrollTo({ top: liveRef.current.scrollHeight });
  }, [liveText]);

  const seconds = estimateSeconds(project.script);
  const hasScript = project.script.trim().length > 0;

  return (
    <div className="flex h-full flex-col overflow-y-auto lg:flex-row lg:overflow-hidden">
      <div className="shrink-0 lg:min-h-0 lg:flex-1 lg:shrink lg:overflow-y-auto">
        <div className="mx-auto max-w-[760px] px-5 py-6 md:px-8">
          {writing ? (
            <div className="rounded-[14px] border border-accent/50 bg-accent-soft/40 p-5">
              <div className="mb-3 flex items-center gap-2 text-[13px] font-medium text-accent-ink">
                <Spinner className="size-3.5 text-accent-ink" />
                {writing.state === "queued" ? "排队中，马上开始写" : "正在写剧本…"}
              </div>
              <div ref={liveRef} className="max-h-[60vh] overflow-y-auto">
                <ScriptHighlight text={liveText} cast={project.characters}>
                  {writing.state === "running" && <span className="caret" />}
                </ScriptHighlight>
              </div>
            </div>
          ) : (
            <ScriptEditor
              value={project.script}
              cast={project.characters}
              onChange={(script) => projectApi.edit((p) => ({ ...p, script }))}
              placeholder={"# 片名\n## 场1 地点 · 时间\n【画面】主角站在……\n角色（情绪）：台词"}
            />
          )}
        </div>
      </div>
      <aside className="shrink-0 border-t border-line bg-surface-2/50 lg:w-[340px] lg:overflow-y-auto lg:border-t-0 lg:border-l">
        <div className="flex flex-col gap-5 p-5">
          <div>
            <div className="text-[13px] text-ink-3">剧本长度</div>
            <div className="tabular mt-1 text-[22px] font-semibold">{hasScript ? `约 ${duration(seconds)}` : "还没有剧本"}</div>
            <div className="mt-0.5 text-[12.5px] text-ink-3">
              目标 {duration(project.target_seconds)}　{project.script.length} 字
            </div>
          </div>
          <Field label="故事梗概">
            <textarea
              value={project.premise}
              onChange={(e) => projectApi.edit((p) => ({ ...p, premise: e.target.value }))}
              rows={4}
              className="field resize-y text-[13.5px] leading-relaxed"
              placeholder="主角是谁、遇到什么冲突、结局往哪走"
            />
          </Field>
          <div className="flex flex-col gap-2">
            <textarea
              value={instructions}
              onChange={(e) => setInstructions(e.target.value)}
              rows={2}
              className="field resize-y text-[13.5px] leading-relaxed"
              placeholder={hasScript ? "改写要求，例如：开头更快进入冲突，结尾加一个反转" : "补充要求（可选）：风格、人物关系、必须出现的情节"}
            />
            <Button
              variant={hasScript ? "secondary" : "primary"}
              icon={hasScript ? <PenLine className="size-4" /> : <Wand2 className="size-4" />}
              loading={Boolean(writing)}
              disabled={!hasScript && !project.premise.trim() && !instructions.trim()}
              onClick={() => {
                void projectApi.director("script", { instructions });
                setInstructions("");
              }}
            >
              {hasScript ? "按要求改写" : "写剧本"}
            </Button>
            <DirectorModelNote value={project.engines.director} />
          </div>
          <div className="border-t border-line pt-5">
            <div className="text-[14px] font-medium">拆分镜</div>
            <p className="mt-1 text-[12.5px] leading-relaxed text-ink-3">
              把剧本拆成镜头：每个镜头有景别、运镜、画面动作、台词和时长，同时整理出角色和场景。
              {project.shots.length > 0 && ` 会替换现有的 ${project.shots.length} 个镜头。`}
            </p>
            {breaking ? (
              <div className="mt-3 rounded-[10px] bg-accent-soft px-3 py-2.5">
                <div className="mb-2 flex items-center gap-2 text-[12.5px] text-accent-ink">
                  <Spinner className="size-3.5 text-accent-ink" />
                  {breaking.progress.stage || (breaking.state === "queued" ? "排队中" : "拆分中")}
                </div>
                <ProgressBar value={breaking.progress.total ? (breaking.progress.done || 0) / breaking.progress.total : 0.1} />
              </div>
            ) : (
              <Button
                className="mt-3 w-full"
                variant={hasScript && !project.shots.length ? "primary" : "secondary"}
                icon={<Clapperboard className="size-4" />}
                disabled={!hasScript || Boolean(writing)}
                onClick={() => {
                  if (project.shots.length && !window.confirm(`重新拆分会替换现有的 ${project.shots.length} 个镜头，确定吗？`)) return;
                  void projectApi.director("breakdown", { replace: true });
                }}
              >
                {project.shots.length ? "重新拆分镜" : "拆分镜"}
              </Button>
            )}
            {lastBreak?.state === "failed" && <p className="mt-2 text-[12.5px] leading-relaxed text-red">{lastBreak.error}</p>}
            {project.shots.length > 0 && !breaking && (
              <button onClick={() => navigate(`/studio/${project.id}/board`)} className={cn("mt-3 text-[13px] text-blue hover:underline")}>
                查看 {project.shots.length} 个镜头
              </button>
            )}
          </div>
        </div>
      </aside>
    </div>
  );
}

/** Which model the director will use, so a cloud choice is visible before writing. */
function DirectorModelNote({ value }: { value: string }) {
  const models = useModels("studio.director");
  const ref = value || models.data?.default || "";
  if (!ref) return null;
  const endpoint = models.data?.endpoints.find((e) => e.id === ref.split("/", 1)[0]);
  return (
    <div className="flex items-center gap-1.5 text-[12px] text-ink-3">
      <span className="truncate">导演模型：{modelLabel(ref)}</span>
      {endpoint?.egress === "cloud" && <CloudBadge />}
    </div>
  );
}
