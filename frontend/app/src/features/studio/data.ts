import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { api, ApiError, upload } from "@/lib/api";
import { duration } from "@/lib/format";
import { subscribeLive } from "@/lib/live";
import { ACTIVE_JOB_STATES, type Job, type Project, type ProjectSummary, type ProjectView, type Slot, type StudioOverview, type Take } from "@/lib/types";

export function useOverview() {
  return useQuery({
    queryKey: ["studio", "overview"],
    queryFn: () => api<StudioOverview>("/v1/studio/overview"),
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
}

export function useProjects() {
  const qc = useQueryClient();
  const query = useQuery({ queryKey: ["studio", "projects"], queryFn: () => api<ProjectSummary[]>("/v1/studio/projects") });
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    return subscribeLive((event) => {
      if (event.type === "job" || event.type === "take" || event.type === "project") {
        if (timer) return;
        timer = setTimeout(() => {
          timer = null;
          void qc.invalidateQueries({ queryKey: ["studio", "projects"] });
        }, 1500);
      }
    });
  }, [qc]);
  return query;
}

export interface ProjectApi {
  view: ProjectView | undefined;
  project: Project | undefined;
  loading: boolean;
  error: unknown;
  saving: boolean;
  dirty: boolean;
  edit: (change: (project: Project) => Project) => void;
  flush: () => Promise<void>;
  takes: (slot: Slot | "voice_sample", target: string) => Take[];
  selected: (slot: Slot | "voice_sample", target: string) => Take | undefined;
  jobsFor: (kind: string, target: string) => Job[];
  activeJobs: Job[];
  select: (slot: Slot, target: string, takeId: string | null) => Promise<void>;
  generate: (kind: string, targetIds: string[], options?: { count?: number; engine?: string; overrides?: Record<string, unknown> }) => Promise<void>;
  director: (kind: "script" | "breakdown" | "prompts", body?: Record<string, unknown>) => Promise<void>;
  render: (quality: "preview" | "final") => Promise<void>;
  autopilot: (body?: Record<string, unknown>) => Promise<void>;
  uploadTo: (slot: Slot, target: string, file: File) => Promise<Take | undefined>;
  removeTake: (takeId: string) => Promise<void>;
  rateTake: (takeId: string, rating: number) => Promise<void>;
  promoteVoice: (takeId: string) => Promise<void>;
  cancelJob: (jobId: string) => Promise<void>;
  retryJob: (jobId: string, batch?: boolean) => Promise<void>;
  approveJob: (job: Job) => Promise<void>;
  reconcileJob: (job: Job) => Promise<void>;
  refresh: () => void;
}

export function useProject(id: string): ProjectApi {
  const qc = useQueryClient();
  const key = useMemo(() => ["studio", "project", id], [id]);
  const query = useQuery({ queryKey: key, queryFn: () => api<ProjectView>(`/v1/studio/projects/${id}`), refetchInterval: 20_000 });
  const [local, setLocal] = useState<Project | undefined>();
  const [saving, setSaving] = useState(false);
  const dirtyRef = useRef(false);
  const localRef = useRef<Project | undefined>(undefined);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [dirty, setDirty] = useState(false);

  // Server copy replaces the local copy unless the user has unsaved edits.
  useEffect(() => {
    if (query.data && !dirtyRef.current) {
      setLocal(query.data.project);
      localRef.current = query.data.project;
    }
  }, [query.data]);

  const refresh = useCallback(() => void qc.invalidateQueries({ queryKey: key }), [qc, key]);

  useEffect(() => {
    let pending: ReturnType<typeof setTimeout> | null = null;
    const soon = () => {
      if (pending) return;
      pending = setTimeout(() => {
        pending = null;
        refresh();
      }, 600);
    };
    const off = subscribeLive((event) => {
      if (event.type === "job" && event.job.project_id === id) {
        qc.setQueryData<ProjectView>(key, (view) => {
          if (!view) return view;
          const jobs = view.jobs.some((j) => j.id === event.job.id)
            ? view.jobs.map((j) => (j.id === event.job.id ? event.job : j))
            : [event.job, ...view.jobs];
          return { ...view, jobs };
        });
        if (!ACTIVE_JOB_STATES.includes(event.job.state)) soon();
      }
      if (event.type === "take" && event.take.project_id === id) soon();
      if (event.type === "project" && event.project_id === id) soon();
    });
    return () => {
      off();
      if (pending) clearTimeout(pending);
    };
  }, [id, qc, key, refresh]);

  const flush = useCallback(async () => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
    const project = localRef.current;
    if (!dirtyRef.current || !project) return;
    setSaving(true);
    try {
      const view = await api<ProjectView>(`/v1/studio/projects/${id}`, {
        method: "PUT",
        body: { revision: project.revision, project },
      });
      dirtyRef.current = false;
      setDirty(false);
      qc.setQueryData(key, view);
      // Keep edits typed while the save was in flight.
      setLocal((current) => {
        const merged = current && current !== project ? { ...current, revision: view.project.revision } : view.project;
        localRef.current = merged;
        if (current && current !== project) {
          dirtyRef.current = true;
          setDirty(true);
        }
        return merged;
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        toast.error("项目在别处被修改，已载入最新版本");
        dirtyRef.current = false;
        setDirty(false);
        refresh();
      } else {
        toast.error(`保存失败：${(error as Error).message}`);
      }
    } finally {
      setSaving(false);
    }
  }, [id, key, qc, refresh]);

  const edit = useCallback(
    (change: (project: Project) => Project) => {
      setLocal((current) => {
        if (!current) return current;
        const next = change(current);
        localRef.current = next;
        return next;
      });
      dirtyRef.current = true;
      setDirty(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => void flush(), 800);
    },
    [flush],
  );

  useEffect(
    () => () => {
      if (dirtyRef.current) void flush();
    },
    [flush],
  );

  const view = query.data;
  const takes = useCallback(
    (slot: string, target: string) => (view?.takes || []).filter((t) => t.slot === slot && t.target_id === target),
    [view],
  );
  const selected = useCallback(
    (slot: string, target: string) => {
      const takeId = view?.selections[`${slot}:${target}`];
      return takeId ? view?.takes.find((t) => t.id === takeId) : undefined;
    },
    [view],
  );
  const jobsFor = useCallback(
    (kind: string, target: string) => (view?.jobs || []).filter((j) => j.kind === kind && j.target_id === target),
    [view],
  );
  const activeJobs = useMemo(() => (view?.jobs || []).filter((j) => ACTIVE_JOB_STATES.includes(j.state)), [view]);

  const run = useCallback(
    async <T,>(promise: Promise<T>, success?: string): Promise<T | undefined> => {
      try {
        const result = await promise;
        if (success) toast.success(success);
        refresh();
        return result;
      } catch (error) {
        toast.error((error as Error).message);
        return undefined;
      }
    },
    [refresh],
  );

  return {
    view,
    project: local,
    loading: query.isLoading,
    error: query.error,
    saving,
    dirty,
    edit,
    flush,
    takes,
    selected,
    jobsFor,
    activeJobs,
    refresh,
    select: async (slot, target, takeId) => {
      qc.setQueryData<ProjectView>(key, (current) => {
        if (!current) return current;
        const selections = { ...current.selections };
        if (takeId) selections[`${slot}:${target}`] = takeId;
        else delete selections[`${slot}:${target}`];
        return { ...current, selections };
      });
      await run(api(`/v1/studio/projects/${id}/select`, { body: { slot, target_id: target, take_id: takeId } }));
    },
    generate: async (kind, targetIds, options = {}) => {
      await flush();
      const result = await run(
        api<{ jobs: Job[]; skipped: string[] }>(`/v1/studio/projects/${id}/generate`, {
          body: { kind, target_ids: targetIds, count: options.count || 1, engine: options.engine, overrides: options.overrides },
        }),
      );
      if (result) {
        const total = result.jobs.reduce((sum, job) => sum + job.estimate_seconds, 0);
        const skipped = result.skipped?.length ? `；${result.skipped.length} 项暂时不能生成：${result.skipped[0]}` : "";
        toast.success(`已加入队列：${result.jobs.length} 项`, { description: `预计 ${duration(total)}${skipped}` });
      }
    },
    director: async (kind, body = {}) => {
      await flush();
      await run(api(`/v1/studio/projects/${id}/director/${kind}`, { body }), "已交给导演助手");
    },
    render: async (quality) => {
      await flush();
      await run(api(`/v1/studio/projects/${id}/render`, { body: { quality } }), quality === "final" ? "开始导出成片" : "开始生成预览");
    },
    autopilot: async (body = {}) => {
      await flush();
      const result = await run(
        api<{ jobs: Job[]; skipped: string[]; estimate: { seconds: number }; message?: string }>(`/v1/studio/projects/${id}/autopilot`, { body }),
      );
      if (result && !result.jobs.length) toast(result.message || "没有需要制作的内容", { description: result.skipped?.join("；") });
      else if (result) toast.success("一键制作已开始", { description: `按当前镜头估算约 ${duration(result.estimate.seconds)}，进度在右上角队列里看` });
    },
    uploadTo: async (slot, target, file) =>
      run(upload<Take>(`/v1/studio/projects/${id}/upload?slot=${slot}&target_id=${encodeURIComponent(target)}`, file, file.name), "已上传"),
    removeTake: async (takeId) => {
      await run(api(`/v1/studio/projects/${id}/takes/${takeId}`, { method: "DELETE" }));
    },
    rateTake: async (takeId, rating) => {
      await run(api(`/v1/studio/projects/${id}/takes/${takeId}/rate`, { body: { rating } }));
    },
    promoteVoice: async (takeId) => {
      await run(api(`/v1/studio/projects/${id}/takes/${takeId}/promote-voice`, { method: "POST" }), "已设为这个角色的参考音");
    },
    cancelJob: async (jobId) => {
      await run(api(`/v1/studio/jobs/${jobId}/cancel`, { method: "POST" }), "已取消这一批里未完成的项目");
    },
    retryJob: async (jobId, batch = false) => {
      await run(api(`/v1/studio/jobs/${jobId}/retry${batch ? "?batch=1" : ""}`, { method: "POST" }), "已重新排队");
    },
    approveJob: async (job) => {
      await run(api(`/v1/approvals/${job.approval_id}/decision`, { body: { decision: "approve" } }), "已批准，开始生成");
    },
    reconcileJob: async (job) => {
      await run(api(`/v1/tasks/${job.run_id}/reconcile`, { body: { step_id: job.step_id } }), "正在核对，结果会接回这一项");
    },
  };
}

export function newId(prefix: string): string {
  return `${prefix}_${Math.random().toString(16).slice(2, 12)}`;
}
