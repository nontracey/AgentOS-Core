import { useQuery } from "@tanstack/react-query";
import { Clapperboard, Download, FileText, FolderOpen, Music, Play, Search, Video } from "lucide-react";
import { useMemo, useState } from "react";
import { Markdown } from "@/components/Markdown";
import { Dialog, Empty, Segmented, Spinner, Switch } from "@/components/ui";
import { api, downloadAsset } from "@/lib/api";
import { bytes, cn, relativeTime } from "@/lib/format";

interface Asset {
  id: string;
  name: string;
  kind: string;
  mime: string;
  size: number;
  run_id: string | null;
  source_node: string;
  created_at: string;
}

type Kind = "all" | "film" | "image" | "video" | "audio" | "doc";

/* Inputs people uploaded and the studio's working files stay with their project; the library shows results. */
const SUPPORTING = new Set(["studio_preview", "studio_poster", "studio_subtitles", "studio_upload", "chat_attachment", "aigc_material", "material"]);

function supporting(asset: Asset): boolean {
  return SUPPORTING.has(asset.kind) || (asset.run_id || "").startsWith("studio-");
}

function kindOf(asset: Asset): Exclude<Kind, "all"> {
  if (asset.kind === "studio_render") return "film";
  if (asset.mime.startsWith("image/")) return "image";
  if (asset.mime.startsWith("video/")) return "video";
  if (asset.mime.startsWith("audio/")) return "audio";
  return "doc";
}

type Links = Record<string, { url: string; thumb?: string }>;

function Preview({ asset, links, onClose }: { asset: Asset; links: Links; onClose: () => void }) {
  const kind = kindOf(asset);
  const url = links[asset.id]?.url;
  const text = useQuery({
    queryKey: ["asset-text", asset.id],
    queryFn: async () => {
      const resp = await fetch(url!);
      return resp.text();
    },
    enabled: kind === "doc" && Boolean(url) && asset.size < 2_000_000,
  });
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && onClose()}
      title={asset.name}
      description={`${bytes(asset.size)}　${relativeTime(asset.created_at)}`}
      width={kind === "doc" ? 760 : 900}
      footer={
        <button onClick={() => void downloadAsset(asset.id, asset.name)} className="inline-flex h-9 items-center gap-2 rounded-[8px] bg-primary px-3.5 text-[14px] font-medium text-on-primary">
          <Download className="size-4" />
          下载
        </button>
      }
    >
      {kind === "image" && url && <img src={url} alt="" className="mx-auto max-h-[70vh] rounded-[8px]" />}
      {(kind === "video" || kind === "film") && url && <video src={url} poster={links[asset.id]?.thumb} controls autoPlay className="mx-auto max-h-[70vh] rounded-[8px] bg-black" />}
      {kind === "audio" && url && <audio src={url} controls autoPlay className="w-full" />}
      {kind === "doc" &&
        (text.isLoading ? (
          <Spinner />
        ) : asset.mime.includes("markdown") || asset.name.endsWith(".md") ? (
          <Markdown source={text.data || ""} />
        ) : (
          <pre className="max-h-[60vh] overflow-auto rounded-[10px] bg-surface-2 p-4 font-mono text-[12.5px] leading-relaxed whitespace-pre-wrap">{text.data || "无法预览，请下载查看"}</pre>
        ))}
    </Dialog>
  );
}

function Tile({ asset, thumb, onOpen }: { asset: Asset; thumb?: string; onOpen: () => void }) {
  const type = kindOf(asset);
  const moving = type === "video" || type === "film";
  return (
    <button onClick={onOpen} className="group overflow-hidden rounded-[12px] border border-line bg-surface text-left transition-[border-color,box-shadow] hover:border-line-strong hover:shadow-[var(--shadow-card)]">
      <div className={cn("relative flex aspect-[4/3] items-center justify-center", moving ? "bg-[#0e1014]" : "bg-surface-2")}>
        {thumb && (type === "image" || moving) ? (
          <img src={thumb} alt="" loading="lazy" className={cn("absolute inset-0 size-full", moving ? "object-contain" : "object-cover")} />
        ) : moving ? (
          <Video className="size-7 text-white/40" />
        ) : type === "audio" ? (
          <Music className="size-7 text-ink-3" />
        ) : (
          <FileText className="size-7 text-ink-3" />
        )}
        {moving && (
          <span className="absolute inset-0 m-auto flex size-10 items-center justify-center rounded-full bg-black/55 text-white opacity-90 backdrop-blur-sm transition-transform group-hover:scale-105">
            <Play className="size-4 translate-x-px" />
          </span>
        )}
        {type === "film" && (
          <span className="absolute top-2 left-2 inline-flex items-center gap-1 rounded-full bg-accent px-2 py-px text-[11px] font-medium text-on-accent">
            <Clapperboard className="size-3" />
            成片
          </span>
        )}
      </div>
      <div className="px-3 py-2">
        <div className="truncate text-[13px] font-medium">{asset.name}</div>
        <div className="tabular mt-0.5 flex justify-between text-[11.5px] text-ink-3">
          <span>{bytes(asset.size)}</span>
          <span>{relativeTime(asset.created_at)}</span>
        </div>
      </div>
    </button>
  );
}

export default function LibraryPage() {
  const [kind, setKind] = useState<Kind>("all");
  const [query, setQuery] = useState("");
  const [everything, setEverything] = useState(false);
  const [open, setOpen] = useState<Asset | null>(null);
  const assets = useQuery({ queryKey: ["assets"], queryFn: () => api<Asset[]>("/v1/assets") });
  const hidden = useMemo(() => (assets.data || []).filter(supporting).length, [assets.data]);
  const filtered = useMemo(
    () =>
      (assets.data || [])
        .filter((a) => everything || !supporting(a))
        .filter((a) => kind === "all" || kindOf(a) === kind || (kind === "video" && kindOf(a) === "film"))
        .filter((a) => !query || a.name.toLowerCase().includes(query.toLowerCase()))
        .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || "")),
    [assets.data, kind, query, everything],
  );
  const visible = filtered.slice(0, 240);
  const urls = useQuery({
    queryKey: ["asset-urls", visible.map((a) => a.id).join(",")],
    queryFn: () => api<{ urls: Links }>("/v1/studio/sign", { body: { asset_ids: visible.map((a) => a.id), width: 420 } }),
    enabled: visible.length > 0,
    staleTime: 30 * 60_000,
  });
  const links = urls.data?.urls || {};

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[1180px] px-6 py-7 md:px-10">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-[24px] font-semibold tracking-tight">成果</h1>
            <p className="mt-1 text-[13.5px] text-ink-3">任务产出的文件和工作室导出的成片。制作中的画面、配音等素材留在各自的作品里。</p>
          </div>
          <div className="relative w-full sm:w-64">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-ink-3" />
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="按名称搜索" className="field h-9 pl-8" />
          </div>
        </div>
        <div className="mt-5 flex flex-wrap items-center justify-between gap-3">
          <Segmented
            value={kind}
            onChange={setKind}
            options={[
              { value: "all", label: "全部" },
              { value: "film", label: "成片" },
              { value: "image", label: "图片" },
              { value: "video", label: "视频" },
              { value: "audio", label: "音频" },
              { value: "doc", label: "文档" },
            ]}
          />
          {hidden > 0 && (
            <label className="flex items-center gap-2.5 text-[13px] text-ink-2">
              显示素材和中间文件（{hidden}）
              <Switch checked={everything} onChange={setEverything} />
            </label>
          )}
        </div>
        {assets.isLoading ? (
          <div className="flex justify-center py-20">
            <Spinner />
          </div>
        ) : filtered.length === 0 ? (
          <Empty icon={<FolderOpen />} title={query ? "没有找到这个名字的文件" : "这里还是空的"}>
            {query ? "换个关键词试试。" : "任务完成后的报告、工作室导出的成片会出现在这里。"}
          </Empty>
        ) : (
          <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
            {visible.map((asset) => (
              <Tile key={asset.id} asset={asset} thumb={links[asset.id]?.thumb} onOpen={() => setOpen(asset)} />
            ))}
          </div>
        )}
        {filtered.length > visible.length && <p className="mt-4 text-center text-[12.5px] text-ink-3">只显示最近 {visible.length} 个，用搜索找更早的</p>}
      </div>
      {open && <Preview asset={open} links={links} onClose={() => setOpen(null)} />}
    </div>
  );
}
