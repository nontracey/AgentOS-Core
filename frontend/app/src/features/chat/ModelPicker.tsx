import * as Popover from "@radix-ui/react-popover";
import { useQuery } from "@tanstack/react-query";
import { Brain, Check, ChevronDown, Eye, RefreshCw, Search, Wrench, WifiOff } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import { bytes, cn, modelLabel } from "@/lib/format";
import type { EndpointInfo, ModelInfo } from "@/lib/types";

export interface ModelsPayload {
  models: ModelInfo[];
  endpoints: EndpointInfo[];
  default: string;
}

export function useModels() {
  return useQuery({
    queryKey: ["chat", "models"],
    queryFn: () => api<ModelsPayload>("/v1/chat/models"),
    staleTime: 20_000,
    refetchInterval: 30_000,
  });
}

export function capabilityBadges(model: ModelInfo) {
  const caps = new Set(model.capabilities);
  return (
    <span className="flex items-center gap-1 text-ink-3">
      {caps.has("thinking") && <Brain aria-label="可深度思考" className="size-3.5" />}
      {caps.has("vision") && <Eye aria-label="能看图" className="size-3.5" />}
      {caps.has("tools") && <Wrench aria-label="支持工具调用" className="size-3.5" />}
    </span>
  );
}

export function ModelPicker({ value, onChange, align = "start" }: { value: string; onChange: (ref: string) => void; align?: "start" | "end" }) {
  const models = useModels();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const current = value || models.data?.default || "";
  const selected = models.data?.models.find((m) => m.ref === current);
  const groups = useMemo(() => {
    const data = models.data;
    if (!data) return [];
    const q = query.trim().toLowerCase();
    return data.endpoints.map((endpoint) => ({
      endpoint,
      models: data.models
        .filter((m) => m.endpoint === endpoint.id && (!q || m.name.toLowerCase().includes(q)))
        .sort((a, b) => Number(b.loaded) - Number(a.loaded) || a.name.localeCompare(b.name)),
    }));
  }, [models.data, query]);

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button className="inline-flex h-8 max-w-[260px] items-center gap-2 rounded-[9px] px-2.5 text-[13.5px] font-medium hover:bg-surface-3">
          <span className={cn("size-2 shrink-0 rounded-full", selected?.loaded ? "bg-green" : "bg-line-strong")} />
          <span className="truncate">{modelLabel(current) || (models.isLoading ? "读取模型…" : "选择模型")}</span>
          <ChevronDown className="size-3.5 shrink-0 text-ink-3" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align={align}
          sideOffset={6}
          className="z-50 w-[min(92vw,380px)] rounded-[14px] border border-line bg-surface shadow-[var(--shadow-pop)]"
        >
          <div className="flex items-center gap-2 border-b border-line px-3 py-2">
            <Search className="size-4 text-ink-3" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索模型"
              className="h-7 flex-1 bg-transparent text-[13.5px] outline-none placeholder:text-ink-3"
            />
            <button onClick={() => void api("/v1/chat/models?refresh=1").then(() => models.refetch())} className="rounded-md p-1 text-ink-3 hover:bg-surface-3 hover:text-ink" aria-label="重新读取">
              <RefreshCw className={cn("size-3.5", models.isFetching && "animate-spin")} />
            </button>
          </div>
          <div className="max-h-[min(60vh,440px)] overflow-y-auto p-1.5">
            {groups.map(({ endpoint, models: list }) => (
              <div key={endpoint.id} className="mb-1">
                <div className="flex items-center justify-between px-2 pt-2 pb-1 text-[12px] text-ink-3">
                  <span className="font-medium text-ink-2">{endpoint.label}</span>
                  <span>{endpoint.online ? endpoint.host : "离线"}</span>
                </div>
                {!endpoint.online && (
                  <div className="mx-2 mb-1 flex items-start gap-2 rounded-[8px] bg-surface-2 px-2.5 py-2 text-[12px] leading-relaxed text-ink-3">
                    <WifiOff className="mt-0.5 size-3.5 shrink-0" />
                    {endpoint.error || "无法连接"}
                  </div>
                )}
                {list.map((model) => (
                  <button
                    key={model.ref}
                    onClick={() => {
                      onChange(model.ref);
                      setOpen(false);
                    }}
                    className={cn(
                      "flex w-full items-center gap-3 rounded-[9px] px-2.5 py-2 text-left hover:bg-surface-3",
                      model.ref === current && "bg-accent-soft hover:bg-accent-soft",
                    )}
                  >
                    <span className={cn("size-2 shrink-0 rounded-full", model.loaded ? "bg-green" : "bg-line")} />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-2">
                        <span className="truncate text-[13.5px] font-medium">{modelLabel(model.name)}</span>
                        {capabilityBadges(model)}
                      </span>
                      <span className="tabular block truncate text-[12px] text-ink-3">
                        {[model.parameter_size, model.quantization, model.size_bytes ? bytes(model.size_bytes) : "", model.context_length ? `${Math.round(model.context_length / 1024)}K 上下文` : ""]
                          .filter(Boolean)
                          .join("　")}
                        {model.loaded && "　已在显存"}
                      </span>
                    </span>
                    {model.ref === current && <Check className="size-4 text-accent-ink" />}
                  </button>
                ))}
                {endpoint.online && list.length === 0 && <div className="px-2.5 py-2 text-[12.5px] text-ink-3">没有匹配的模型</div>}
              </div>
            ))}
            {models.isError && <div className="p-3 text-[13px] text-red">{(models.error as Error).message}</div>}
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
