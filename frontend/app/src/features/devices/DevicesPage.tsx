import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Cpu, HardDrive, MonitorSmartphone, RefreshCw, Server, X } from "lucide-react";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import { bytes, cn, duration, relativeTime } from "@/lib/format";
import type { EndpointInfo, GpuSnapshot, ModelInfo, StudioOverview } from "@/lib/types";
import { Badge, Button, Spinner } from "@/components/ui";
import { useGpu } from "@/components/Shell";
import { useOverview } from "@/features/studio/data";
import { GrantButtons } from "@/features/studio/StudioHome";

interface ChatStatus {
  endpoints: (EndpointInfo & { model_count: number; loaded: ModelInfo[] })[];
  gpu: GpuSnapshot;
}

interface NodeRow {
  node_id: string;
  os: string;
  arch: string;
  status: string;
  last_heartbeat: string | null;
  endpoint?: string;
}

function Section({ icon, title, aside, children }: { icon: ReactNode; title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="panel p-5">
      <div className="mb-4 flex items-center gap-2.5">
        <span className="text-ink-3 [&>svg]:size-[18px]">{icon}</span>
        <h2 className="text-[15px] font-semibold">{title}</h2>
        <div className="ml-auto">{aside}</div>
      </div>
      {children}
    </section>
  );
}

function until(iso: string): string {
  return new Date(iso).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/* Owner pre-authorizations for the studio's node tools: visible, time-bounded and revocable. */
function Permissions({ overview }: { overview: StudioOverview }) {
  const qc = useQueryClient();
  const { tools, needs_approval, can_grant } = overview.permissions;
  if (!tools.length) return null;
  const granted = tools.filter((t) => t.grant);
  async function revoke() {
    for (const item of granted) if (item.grant) await api(`/v1/grants/${item.grant.id}/revoke`, { method: "POST" });
    void qc.invalidateQueries({ queryKey: ["studio", "overview"] });
  }
  return (
    <div className="mb-4 flex flex-col gap-2.5 rounded-[10px] bg-surface-2 px-3.5 py-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[13px]">
        <span className="font-medium">工作室直接使用</span>
        {tools.map((item) => (
          <span key={item.tool} className="text-ink-2">
            {item.label}
            <span className="ml-1 text-ink-3">{item.grant ? `到 ${until(item.grant.expires_at)}` : "每次批准"}</span>
          </span>
        ))}
      </div>
      {can_grant && (
        <div className="flex flex-wrap items-center gap-2">
          {needs_approval && <GrantButtons />}
          {granted.length > 0 && (
            <Button size="sm" variant="ghost" onClick={() => void revoke()}>
              撤销授权
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

export default function DevicesPage() {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["chat", "status"], queryFn: () => api<ChatStatus>("/v1/chat/status"), refetchInterval: 10_000 });
  const overview = useOverview();
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: () => api<NodeRow[]>("/v1/nodes"), refetchInterval: 20_000 });
  const gpu = useGpu() || status.data?.gpu;
  const queue = overview.data?.queue;

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-[1000px] flex-col gap-5 px-6 py-7 md:px-10">
        <div className="flex items-end justify-between gap-4">
          <div>
            <h1 className="text-[24px] font-semibold tracking-tight">设备</h1>
            <p className="mt-1 text-[13.5px] text-ink-3">推理模型、生成节点和显卡队列的实时状态。</p>
          </div>
          <Button
            size="sm"
            icon={<RefreshCw className={cn("size-3.5", status.isFetching && "animate-spin")} />}
            onClick={() => {
              void api("/v1/chat/models?refresh=1").then(() => qc.invalidateQueries({ queryKey: ["chat"] }));
              void qc.invalidateQueries({ queryKey: ["studio", "overview"] });
              void qc.invalidateQueries({ queryKey: ["nodes"] });
            }}
          >
            重新检测
          </Button>
        </div>

        <Section icon={<Cpu />} title="显卡">
          {gpu?.busy && gpu.holder ? (
            <div className="live-glow flex items-center gap-3 rounded-[12px] bg-accent-soft px-4 py-3">
              <Spinner className="text-accent-ink" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-[14px] font-medium text-accent-ink">{gpu.holder.label}</div>
                <div className="text-[12.5px] text-ink-3">
                  {gpu.holder.kind === "chat" ? "对话正在使用显卡" : "制作任务正在使用显卡"}　已用 {duration(gpu.holder.elapsed_seconds)}
                </div>
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-3 rounded-[12px] bg-surface-2 px-4 py-3 text-[14px]">
              <span className="size-2.5 rounded-full bg-green" />
              空闲，对话和制作任务都可以马上开始
            </div>
          )}
          {gpu && gpu.waiting.length > 0 && (
            <div className="mt-3 flex flex-col gap-1">
              {gpu.waiting.map((item, i) => (
                <div key={i} className="flex items-center justify-between rounded-[8px] px-2 py-1 text-[13px] text-ink-2">
                  <span>{item.label}</span>
                  <span className="text-[12px] text-ink-3">已等 {duration(item.waited_seconds)}</span>
                </div>
              ))}
            </div>
          )}
          {queue && (queue.queued > 0 || queue.running.length > 0) && (
            <p className="mt-3 text-[12.5px] text-ink-3">
              制作队列还有 {queue.queued + queue.running.length} 项，预计 {duration(queue.eta_seconds)}。共用显卡时，对话和生成按先来后到使用。
            </p>
          )}
        </Section>

        <Section icon={<Server />} title="推理模型">
          {status.isLoading && <Spinner />}
          <div className="flex flex-col gap-3">
            {status.data?.endpoints.map((endpoint) => (
              <div key={endpoint.id} className="rounded-[12px] border border-line p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={cn("size-2.5 rounded-full", endpoint.online ? "bg-green" : "bg-red")} />
                  <span className="text-[14px] font-medium">{endpoint.label}</span>
                  <span className="text-[12.5px] text-ink-3">{endpoint.host}</span>
                  <Badge tone="outline">{endpoint.kind === "ollama" ? "Ollama" : "OpenAI 兼容"}</Badge>
                  {endpoint.gpu_shared && <Badge tone="accent">与制作共用显卡</Badge>}
                  <span className="ml-auto text-[12.5px] text-ink-3">{endpoint.online ? `${endpoint.model_count} 个模型` : "离线"}</span>
                </div>
                {!endpoint.online && <p className="mt-2 text-[12.5px] leading-relaxed text-red">{endpoint.error}</p>}
                {endpoint.loaded.length > 0 && (
                  <div className="mt-3 flex flex-col gap-1.5">
                    {endpoint.loaded.map((model) => (
                      <div key={model.ref} className="flex items-center gap-2 text-[13px]">
                        <HardDrive className="size-3.5 text-green" />
                        <span className="font-medium">{model.name}</span>
                        <span className="text-ink-3">在显存　{bytes(model.vram_bytes)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </Section>

        <Section
          icon={<MonitorSmartphone />}
          title="生成节点"
          aside={
            overview.data && (
              <Badge tone={overview.data.node.state === "online" ? "green" : overview.data.node.state === "offline" ? "red" : "neutral"}>
                {{ online: "在线", offline: "离线", unpaired: "未配对", unconfigured: "未指定" }[overview.data.node.state]}
              </Badge>
            )
          }
        >
          {overview.data && (
            <>
              <p className="mb-4 text-[13px] leading-relaxed text-ink-3">
                {overview.data.node.id ? `节点「${overview.data.node.id}」` : "部署还没有指定生成节点。"}
                {overview.data.node.last_seen ? `最近一次响应：${relativeTime(overview.data.node.last_seen)}。` : ""}
                出图、视频、配音和口型在生成节点自己的队列里执行，每一步都按授权与审批规则记录；成片在控制面合成
                {overview.data.ffmpeg ? "（已找到 FFmpeg）。" : "，需要先安装 FFmpeg 或在部署配置里指定它的位置。"}
              </p>
              <Permissions overview={overview.data} />
              <div className="grid gap-2 sm:grid-cols-2">
                {overview.data.engines.map((engine) => (
                  <div key={engine.id} className="flex items-start gap-2.5 rounded-[10px] bg-surface-2 px-3 py-2.5">
                    {engine.available ? <Check className="mt-0.5 size-4 shrink-0 text-green" /> : <X className="mt-0.5 size-4 shrink-0 text-ink-3" />}
                    <div className="min-w-0">
                      <div className="text-[13px] font-medium">{engine.label}</div>
                      <div className="text-[12px] leading-snug text-ink-3">{engine.available ? engine.note : engine.reason}</div>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </Section>

        <Section icon={<Server />} title="已配对设备">
          <div className="flex flex-col gap-1">
            {(nodes.data || []).map((node) => (
              <div key={node.node_id} className="flex items-center gap-3 rounded-[10px] px-3 py-2 text-[13.5px] hover:bg-surface-2">
                <span className={cn("size-2 rounded-full", node.status === "paired" ? "bg-green" : "bg-ink-3")} />
                <span className="font-medium">{node.node_id}</span>
                <span className="text-ink-3">
                  {node.os} {node.arch}
                </span>
                <span className="ml-auto text-[12px] text-ink-3">{node.last_heartbeat ? relativeTime(node.last_heartbeat) : "还没有心跳"}</span>
              </div>
            ))}
            {nodes.data?.length === 0 && <p className="text-[13px] text-ink-3">还没有配对设备。配对和撤销在旧版界面的「能力中心 → 执行环境」里。</p>}
          </div>
        </Section>
      </div>
    </div>
  );
}
