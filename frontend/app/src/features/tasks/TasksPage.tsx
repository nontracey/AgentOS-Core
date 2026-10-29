import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUp, Ban, CheckCircle2, CircleDashed, Download, ExternalLink, FileText, ListChecks, ShieldCheck, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { api, downloadAsset, sse } from "@/lib/api";
import { bytes, cn, relativeTime } from "@/lib/format";
import { Markdown } from "@/components/Markdown";
import { Badge, Button, Empty, Spinner } from "@/components/ui";
import { useIsPhone } from "@/hooks/useMedia";

interface Run {
  id: string;
  state: string;
  goal_text: string;
  created_at: string;
  updated_at: string;
}

interface Step {
  id: string;
  tool_name: string | null;
  state: string;
  started_at: string | null;
  finished_at: string | null;
  error_json?: string | null;
}

interface Approval {
  id: string;
  call_id: string;
  summary: string;
  expires_at: string;
}

interface Artifact {
  id: string;
  name: string;
  mime: string;
  size: number;
  created_at: string;
}

interface TaskDetail {
  run: Run;
  steps: Step[];
  approvals: Approval[];
  artifacts: Artifact[];
  messages: { role: string; content: string }[];
  error: { code?: string; message?: string } | null;
}

const STATE: Record<string, { label: string; tone: "neutral" | "accent" | "blue" | "green" | "red" }> = {
  created: { label: "已受理", tone: "neutral" },
  planning: { label: "规划中", tone: "blue" },
  running: { label: "执行中", tone: "accent" },
  awaiting_approval: { label: "等你确认", tone: "accent" },
  awaiting_input: { label: "等你补充", tone: "accent" },
  awaiting_review: { label: "等你审阅", tone: "accent" },
  awaiting_resources: { label: "等待资源", tone: "neutral" },
  paused: { label: "已暂停", tone: "neutral" },
  cancel_requested: { label: "取消中", tone: "neutral" },
  succeeded: { label: "已完成", tone: "green" },
  failed: { label: "失败", tone: "red" },
  cancelled: { label: "已取消", tone: "neutral" },
};

const ACTIVE = new Set(["created", "planning", "running", "awaiting_resources", "cancel_requested"]);

function StateBadge({ state }: { state: string }) {
  const info = STATE[state] || { label: state, tone: "neutral" as const };
  return <Badge tone={info.tone}>{info.label}</Badge>;
}

function NewTask({ onCreated }: { onCreated: (id: string) => void }) {
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit() {
    if (!goal.trim()) return;
    setBusy(true);
    try {
      const receipt = await api<{ run_id: string }>("/v1/tasks", {
        body: { payload: { goal: goal.trim() }, idempotency_key: crypto.randomUUID() },
      });
      setGoal("");
      onCreated(receipt.run_id);
    } catch (error) {
      toast.error((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="rounded-[14px] border border-line bg-surface p-2 focus-within:border-line-strong">
      <textarea
        value={goal}
        onChange={(e) => setGoal(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void submit();
        }}
        rows={2}
        placeholder="交给 Agent 执行的任务：读材料写报告、整理数据、调用已接入的工具……"
        className="block w-full resize-none bg-transparent px-2 py-1.5 text-[14px] leading-relaxed outline-none placeholder:text-ink-3"
      />
      <div className="flex items-center justify-between px-1">
        <span className="text-[11.5px] text-ink-3">⌘/Ctrl + Enter 提交；需要你确认的操作会先停下来</span>
        <Button size="sm" variant="primary" loading={busy} disabled={!goal.trim()} onClick={() => void submit()} icon={<ArrowUp className="size-3.5" />}>
          开始
        </Button>
      </div>
    </div>
  );
}

function TaskDetailView({ id }: { id: string }) {
  const qc = useQueryClient();
  const detail = useQuery({ queryKey: ["task", id], queryFn: () => api<TaskDetail>(`/v1/tasks/${id}`) });
  const [followup, setFollowup] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    void (async () => {
      try {
        for await (const _event of sse(`/v1/tasks/${id}/stream?follow=1`, { signal: controller.signal })) {
          if (timer) continue;
          timer = setTimeout(() => {
            timer = null;
            void qc.invalidateQueries({ queryKey: ["task", id] });
            void qc.invalidateQueries({ queryKey: ["tasks"] });
          }, 400);
        }
      } catch {
        /* stream closed */
      }
      void qc.invalidateQueries({ queryKey: ["task", id] });
    })();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [id, qc]);

  const decide = useMutation({
    mutationFn: ({ approval, decision }: { approval: string; decision: "approve" | "reject" }) =>
      api(`/v1/approvals/${approval}/decision`, { body: { decision } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["task", id] }),
    onError: (error) => toast.error((error as Error).message),
  });
  const control = useMutation({
    mutationFn: (action: "cancel" | "resume") => api(`/v1/tasks/${id}/${action}`, { method: "POST" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["task", id] }),
    onError: (error) => toast.error((error as Error).message),
  });
  const ask = useMutation({
    mutationFn: () => api(`/v1/tasks/${id}/followups`, { body: { message: followup, idempotency_key: crypto.randomUUID() } }),
    onSuccess: () => {
      setFollowup("");
      void qc.invalidateQueries({ queryKey: ["task", id] });
    },
    onError: (error) => toast.error((error as Error).message),
  });

  if (detail.isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }
  if (!detail.data) return <Empty title="任务不存在" />;
  const { run, steps, approvals, artifacts, messages, error } = detail.data;
  const answer = messages.filter((m) => m.role === "assistant").map((m) => m.content).filter(Boolean).at(-1) || "";
  const active = ACTIVE.has(run.state);
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[820px] px-5 py-6 md:px-8">
        <div className="flex items-start gap-3">
          <h2 className="min-w-0 flex-1 text-[18px] leading-snug font-semibold">{run.goal_text}</h2>
          <StateBadge state={run.state} />
        </div>
        <div className="mt-1.5 flex items-center gap-3 text-[12.5px] text-ink-3">
          <span>{relativeTime(run.created_at)}</span>
          {active && (
            <button onClick={() => control.mutate("cancel")} className="inline-flex items-center gap-1 hover:text-red">
              <Ban className="size-3.5" />
              取消
            </button>
          )}
          {run.state === "paused" && (
            <button onClick={() => control.mutate("resume")} className="hover:text-ink">
              继续执行
            </button>
          )}
          <a href={`/ui/legacy/#/tasks/${run.id}`} className="ml-auto inline-flex items-center gap-1 hover:text-ink">
            在旧版界面查看执行图
            <ExternalLink className="size-3" />
          </a>
        </div>

        {approvals.map((approval) => (
          <div key={approval.id} className="mt-5 rounded-[14px] border border-accent bg-accent-soft/50 p-4">
            <div className="flex items-center gap-2 text-[14px] font-semibold text-accent-ink">
              <ShieldCheck className="size-4" />
              需要你确认
            </div>
            <p className="mt-2 text-[13.5px] leading-relaxed whitespace-pre-wrap">{approval.summary || "Agent 准备执行一个有副作用的操作。"}</p>
            <div className="mt-3 flex gap-2">
              <Button size="sm" variant="primary" loading={decide.isPending} onClick={() => decide.mutate({ approval: approval.id, decision: "approve" })}>
                同意执行
              </Button>
              <Button size="sm" variant="ghost" onClick={() => decide.mutate({ approval: approval.id, decision: "reject" })}>
                拒绝
              </Button>
            </div>
          </div>
        ))}

        {answer ? (
          <div className="mt-6">
            <Markdown source={answer} />
          </div>
        ) : active ? (
          <div className="mt-6 flex items-center gap-2 text-[13.5px] text-ink-3">
            <Spinner />
            Agent 正在处理…
          </div>
        ) : null}

        {error && run.state === "failed" && (
          <div className="mt-5 rounded-[12px] bg-red-soft px-4 py-3 text-[13px] leading-relaxed text-red">{error.message || error.code || "执行失败"}</div>
        )}

        {artifacts.length > 0 && (
          <div className="mt-6">
            <h3 className="mb-2 text-[13.5px] font-semibold">成果</h3>
            <div className="flex flex-col gap-1">
              {artifacts.map((artifact) => (
                <button key={artifact.id} onClick={() => void downloadAsset(artifact.id, artifact.name || "成果")} className="flex items-center gap-3 rounded-[10px] px-3 py-2 text-left hover:bg-surface-2">
                  <FileText className="size-4 text-ink-3" />
                  <span className="min-w-0 flex-1 truncate text-[13.5px]">{artifact.name || artifact.id}</span>
                  <span className="text-[12px] text-ink-3">{bytes(artifact.size)}</span>
                  <Download className="size-4 text-ink-3" />
                </button>
              ))}
            </div>
          </div>
        )}

        {steps.length > 0 && (
          <details className="mt-6 rounded-[12px] border border-line" open={active}>
            <summary className="cursor-pointer px-4 py-2.5 text-[13.5px] font-medium select-none">执行记录　{steps.length} 步</summary>
            <ol className="border-t border-line px-4 py-2">
              {steps.map((step) => (
                <li key={step.id} className="flex items-center gap-2.5 py-1.5 text-[13px]">
                  {step.state === "completed" ? (
                    <CheckCircle2 className="size-4 text-green" />
                  ) : step.state === "failed" || step.state === "uncertain" ? (
                    <XCircle className="size-4 text-red" />
                  ) : (
                    <CircleDashed className={cn("size-4 text-ink-3", step.state === "dispatched" && "animate-spin")} />
                  )}
                  <span className="font-mono text-[12.5px]">{step.tool_name || "思考"}</span>
                  <span className="ml-auto text-[12px] text-ink-3">{step.state}</span>
                </li>
              ))}
            </ol>
          </details>
        )}

        {!active && (
          <div className="mt-6 flex gap-2">
            <input value={followup} onChange={(e) => setFollowup(e.target.value)} placeholder="继续追问或要求修改" className="field h-10" onKeyDown={(e) => e.key === "Enter" && followup.trim() && ask.mutate()} />
            <Button variant="primary" className="h-10" loading={ask.isPending} disabled={!followup.trim()} onClick={() => ask.mutate()}>
              发送
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

export default function TasksPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const phone = useIsPhone();
  const runs = useQuery({ queryKey: ["tasks"], queryFn: () => api<Run[]>("/v1/runs"), refetchInterval: 8000 });
  const approvals = useQuery({ queryKey: ["approvals"], queryFn: () => api<Approval[]>("/v1/approvals"), refetchInterval: 8000 });
  const waiting = new Set((approvals.data || []).map((a) => a.call_id.split(":")[0]));

  const list = (
    <div className="flex h-full flex-col">
      <div className="p-4">
        <NewTask onCreated={(runId) => navigate(`/tasks/${runId}`)} />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-4">
        {(runs.data || []).map((run) => (
          <button
            key={run.id}
            onClick={() => navigate(`/tasks/${run.id}`)}
            className={cn("flex w-full flex-col gap-1 rounded-[10px] px-3 py-2.5 text-left hover:bg-surface-3", run.id === id && "bg-surface-3")}
          >
            <span className="line-clamp-2 text-[13.5px] leading-snug">{run.goal_text}</span>
            <span className="flex items-center gap-2 text-[12px] text-ink-3">
              <StateBadge state={waiting.has(run.id) ? "awaiting_approval" : run.state} />
              {relativeTime(run.updated_at || run.created_at)}
            </span>
          </button>
        ))}
        {runs.data?.length === 0 && <div className="px-4 py-10 text-center text-[13px] text-ink-3">还没有任务</div>}
      </div>
    </div>
  );

  if (phone) {
    return id ? (
      <div className="flex h-full flex-col">
        <button onClick={() => navigate("/tasks")} className="h-11 shrink-0 border-b border-line px-4 text-left text-[13.5px] text-ink-2">
          返回任务列表
        </button>
        <div className="min-h-0 flex-1">
          <TaskDetailView id={id} />
        </div>
      </div>
    ) : (
      list
    );
  }

  return (
    <div className="flex h-full">
      <div className="w-[340px] shrink-0 border-r border-line bg-surface-2/50">{list}</div>
      <div className="min-w-0 flex-1">
        {id ? (
          <TaskDetailView key={id} id={id} />
        ) : (
          <Empty icon={<ListChecks />} title="Agent 任务">
            适合多步骤、要调用工具或需要你确认的工作。日常问答直接用「对话」更快。
          </Empty>
        )}
      </div>
    </div>
  );
}
