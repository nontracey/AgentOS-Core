import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ChevronLeft, Download, MoreHorizontal, PanelLeft, PanelLeftClose, Pin, PinOff, Plus, Search, SlidersHorizontal, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { cn, dayGroup } from "@/lib/format";
import type { Attachment, ChatMessage, ChatParams, Conversation, Preset } from "@/lib/types";
import { useIsPhone } from "@/hooks/useMedia";
import { Button, Dialog, Field, IconButton, Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger, Segmented, Select, Slider, Spinner } from "@/components/ui";
import { Composer } from "./Composer";
import { attach, detach, isLive, lastErrors, onFinished, send, useAnyLive, useLive } from "./liveStore";
import { AssistantMessage, UserMessage } from "./MessageView";
import { ModelPicker, useModels } from "./ModelPicker";

interface Detail extends Omit<Conversation, "live"> {
  messages: ChatMessage[];
  live: { status: string } | null;
}

const DEFAULT_PARAMS: ChatParams = { temperature: 0.7, top_p: 0.9, num_ctx: 8192, think: "auto", keep_alive: "10m" };

function usePresets() {
  return useQuery({ queryKey: ["chat", "presets"], queryFn: () => api<Preset[]>("/v1/chat/presets"), staleTime: 60_000 });
}

function savedDefaultModel(): string {
  try {
    return localStorage.getItem("agentos_chat_model") || "";
  } catch {
    return "";
  }
}

/* ---------------------------------------------------------------- list */
function ConversationList({ activeId, onPick, onHide }: { activeId?: string; onPick?: () => void; onHide?: () => void }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [query, setQuery] = useState("");
  const live = useAnyLive();
  const list = useQuery({
    queryKey: ["chat", "conversations", query],
    queryFn: () => api<Conversation[]>(`/v1/chat/conversations?q=${encodeURIComponent(query)}`),
    refetchInterval: live.length ? 4000 : 20000,
  });
  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<Conversation> }) => api(`/v1/chat/conversations/${id}`, { method: "PATCH", body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["chat"] }),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api(`/v1/chat/conversations/${id}`, { method: "DELETE" }),
    onSuccess: (_d, id) => {
      void qc.invalidateQueries({ queryKey: ["chat", "conversations"] });
      if (id === activeId) navigate("/chat");
    },
  });
  const groups = useMemo(() => {
    const out: { label: string; items: Conversation[] }[] = [];
    for (const item of list.data || []) {
      const label = item.pinned ? "置顶" : dayGroup(item.updated_at);
      const group = out.find((g) => g.label === label);
      if (group) group.items.push(item);
      else out.push({ label, items: [item] });
    }
    return out;
  }, [list.data]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 px-3 pt-3 pb-2">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-ink-3" />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索对话" className="field h-9 pl-8 text-[13.5px]" />
        </div>
        <IconButton
          label="新对话"
          onClick={() => {
            navigate("/chat");
            onPick?.();
          }}
        >
          <Plus className="size-[18px]" />
        </IconButton>
        {onHide && (
          <IconButton label="收起对话列表" onClick={onHide}>
            <PanelLeftClose className="size-[17px]" />
          </IconButton>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {list.isLoading && (
          <div className="space-y-2 px-1 pt-2">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="skeleton h-9 rounded-[9px]" />
            ))}
          </div>
        )}
        {groups.map((group) => (
          <div key={group.label} className="mt-2">
            <div className="px-2.5 pt-1 pb-1 text-[12px] font-medium text-ink-3">{group.label}</div>
            {group.items.map((item) => (
              <div
                key={item.id}
                className={cn(
                  "group relative flex items-center rounded-[9px] hover:bg-surface-3",
                  item.id === activeId && "bg-surface-3",
                )}
              >
                <button
                  onClick={() => {
                    navigate(`/chat/${item.id}`);
                    onPick?.();
                  }}
                  className="min-w-0 flex-1 px-2.5 py-2 text-left"
                >
                  <span className="flex items-center gap-2">
                    {(item.live || live.includes(item.id)) && <span className="animate-rim size-1.5 shrink-0 rounded-full bg-accent" />}
                    <span className="truncate text-[13.5px]">{item.title || "新对话"}</span>
                  </span>
                </button>
                <Menu>
                  <MenuTrigger asChild>
                    <button aria-label="对话操作" className="mr-1 rounded-md p-1 text-ink-3 opacity-0 group-hover:opacity-100 hover:bg-line hover:text-ink focus:opacity-100 data-[state=open]:opacity-100">
                      <MoreHorizontal className="size-4" />
                    </button>
                  </MenuTrigger>
                  <MenuContent>
                    <MenuItem icon={item.pinned ? <PinOff /> : <Pin />} onSelect={() => update.mutate({ id: item.id, body: { pinned: !item.pinned } })}>
                      {item.pinned ? "取消置顶" : "置顶"}
                    </MenuItem>
                    <MenuItem
                      icon={<Download />}
                      onSelect={() =>
                        void api<string>(`/v1/chat/conversations/${item.id}/export`).then((text) => {
                          const url = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
                          const a = document.createElement("a");
                          a.href = url;
                          a.download = `${item.title || "对话"}.md`;
                          a.click();
                          setTimeout(() => URL.revokeObjectURL(url), 3000);
                        })
                      }
                    >
                      导出为 Markdown
                    </MenuItem>
                    <MenuSeparator />
                    <MenuItem icon={<Trash2 />} danger onSelect={() => remove.mutate(item.id)}>
                      删除
                    </MenuItem>
                  </MenuContent>
                </Menu>
              </div>
            ))}
          </div>
        ))}
        {list.data?.length === 0 && <div className="px-3 py-8 text-center text-[13px] text-ink-3">{query ? "没有找到相关对话" : "还没有对话"}</div>}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- settings */
function SettingsDialog({
  open,
  onOpenChange,
  conversation,
  onSave,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  conversation: Pick<Conversation, "params" | "system_prompt" | "preset">;
  onSave: (body: Partial<Conversation>) => void;
}) {
  const presets = usePresets();
  const [params, setParams] = useState<ChatParams>({ ...DEFAULT_PARAMS, ...conversation.params });
  const [system, setSystem] = useState(conversation.system_prompt);
  const [preset, setPreset] = useState(conversation.preset || "assistant");
  useEffect(() => {
    if (open) {
      setParams({ ...DEFAULT_PARAMS, ...conversation.params });
      setSystem(conversation.system_prompt);
      setPreset(conversation.preset || "assistant");
    }
  }, [open, conversation]);
  const presetPrompt = presets.data?.find((p) => p.id === preset)?.system_prompt || "";
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="对话设置"
      description="只影响这条对话。"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            variant="primary"
            onClick={() => {
              onSave({ params, system_prompt: system, preset });
              onOpenChange(false);
            }}
          >
            保存
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-5">
        <Field label="角色预设">
          <Select value={preset} onChange={setPreset} options={(presets.data || []).map((p) => ({ value: p.id, label: p.name, hint: p.description }))} />
        </Field>
        <Field label="系统提示词" hint={system ? "已自定义，会替代预设的提示词。" : "留空时使用预设的提示词。"}>
          <textarea value={system} onChange={(e) => setSystem(e.target.value)} placeholder={presetPrompt} rows={5} className="field resize-y text-[13.5px] leading-relaxed" />
        </Field>
        <Field label={`温度　${params.temperature.toFixed(2)}`} hint="越低越稳定，越高越有创意。写代码、做分析用 0.2–0.5，写故事用 0.8–1.0。">
          <Slider value={params.temperature} min={0} max={1.5} step={0.05} onChange={(v) => setParams({ ...params, temperature: v })} />
        </Field>
        <Field label="上下文长度" hint="越长能记住越多历史，但更占显存和内存。">
          <Segmented
            value={String(params.num_ctx)}
            onChange={(v) => setParams({ ...params, num_ctx: Number(v) })}
            options={[4096, 8192, 16384, 32768].map((n) => ({ value: String(n), label: `${n / 1024}K` }))}
          />
        </Field>
        <Field label="深度思考">
          <Segmented
            value={params.think}
            onChange={(v) => setParams({ ...params, think: v })}
            options={[
              { value: "auto", label: "自动" },
              { value: "on", label: "开" },
              { value: "off", label: "关" },
            ]}
          />
        </Field>
        <Field label="模型保留时间" hint="回答后模型在显存里保留多久。制作任务排队时会自动立即释放。">
          <Segmented
            value={params.keep_alive}
            onChange={(v) => setParams({ ...params, keep_alive: v })}
            options={["1m", "10m", "30m", "-1"].map((v) => ({ value: v, label: v === "-1" ? "一直" : v.replace("m", " 分钟") }))}
          />
        </Field>
      </div>
    </Dialog>
  );
}

/* ---------------------------------------------------------------- thread */
const SUGGESTIONS: Record<string, string[]> = {
  assistant: ["帮我把这段话改得更简洁有力", "解释一下 MoE 模型为什么在小显存上更快", "列一个周末学习计划"],
  screenwriter: ["写一个 60 秒竖屏短剧开场：末班车晚点的雨夜车站", "给主角设计三句有记忆点的台词", "把这段剧情加一个反转"],
  storyboard: ["把下面这场戏拆成镜头表", "这场追逐戏怎样分镜才有冲击力"],
  "image-prompt": ["雨夜车站，女孩撑伞望向远处的车灯，写成生图提示词", "给这张图写一段图生视频的运动提示词"],
  coder: ["写一个 Python 脚本批量重命名文件", "解释这段报错的原因"],
  translator: ["把这段中文翻译成自然的英文"],
};

export function ChatPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const phone = useIsPhone();
  const [showList, setShowList] = useState(!phone);
  const models = useModels();
  const presets = usePresets();
  const detail = useQuery({
    queryKey: ["chat", "detail", id],
    queryFn: () => api<Detail>(`/v1/chat/conversations/${id}`),
    enabled: Boolean(id),
  });
  const live = useLive(id);
  const [draftModel, setDraftModel] = useState(savedDefaultModel);
  const [draftPreset, setDraftPreset] = useState("assistant");
  const [draftThink, setDraftThink] = useState<ChatParams["think"]>("auto");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);

  useEffect(() => setShowList(!phone), [phone]);

  useEffect(() => {
    return onFinished((cid) => {
      void qc.invalidateQueries({ queryKey: ["chat", "detail", cid] });
      void qc.invalidateQueries({ queryKey: ["chat", "conversations"] });
      void qc.invalidateQueries({ queryKey: ["chat", "models"] });
    });
  }, [qc]);

  useEffect(() => {
    if (id && detail.data?.live && !isLive(id)) attach(id);
  }, [id, detail.data?.live]);

  useEffect(() => () => {
    // Leaving a conversation keeps the server generating; the list shows it as live.
    if (id) detach(id);
  }, [id]);

  const conversation = detail.data;
  const modelRef = (id ? conversation?.model : draftModel) || models.data?.default || "";
  const modelInfo = models.data?.models.find((m) => m.ref === modelRef);
  const caps = new Set(modelInfo?.capabilities || []);
  const think = (id ? conversation?.params?.think : draftThink) || "auto";

  const patchConversation = useMutation({
    mutationFn: (body: Partial<Conversation>) => api(`/v1/chat/conversations/${id}`, { method: "PATCH", body }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["chat", "detail", id] });
      void qc.invalidateQueries({ queryKey: ["chat", "conversations"] });
    },
  });

  function scrollToBottom(smooth = true) {
    const el = scroller.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }

  useEffect(() => {
    if (atBottom) scrollToBottom(false);
  }, [live?.content, live?.thinking, conversation?.messages?.length, atBottom]);

  useEffect(() => {
    setAtBottom(true);
    setTimeout(() => scrollToBottom(false), 30);
  }, [id]);

  async function handleSend(text: string, attachments: Attachment[]) {
    let cid = id;
    if (!cid) {
      try {
        const created = await api<Conversation>("/v1/chat/conversations", {
          body: { model: modelRef, preset: draftPreset, params: { ...DEFAULT_PARAMS, think: draftThink } },
        });
        cid = created.id;
        navigate(`/chat/${cid}`, { replace: true });
      } catch (error) {
        toast.error((error as Error).message);
        return;
      }
    }
    setAtBottom(true);
    send(cid, { content: text, attachments }, text);
    void qc.invalidateQueries({ queryKey: ["chat", "conversations"] });
  }

  const messages = conversation?.messages || [];
  const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant");
  const pendingUser = live && !live.userMessage && live.pendingText ? live.pendingText : "";
  const showLiveTail = live && (!live.assistantId || !messages.some((m) => m.id === live.assistantId));
  const errorBanner = id ? lastErrors.get(id) : "";

  const thread = (
    <div className="flex h-full min-w-0 flex-1 flex-col">
      <header className="flex h-12 shrink-0 items-center gap-1.5 border-b border-line px-3">
        {(!showList || phone) && (
          <IconButton label={phone ? "对话列表" : "显示对话列表"} onClick={() => setShowList(true)}>
            {phone ? <ChevronLeft className="size-[18px]" /> : <PanelLeft className="size-[18px]" />}
          </IconButton>
        )}
        {id && conversation ? (
          renaming ? (
            <input
              autoFocus
              defaultValue={conversation.title}
              onBlur={(e) => {
                setRenaming(false);
                if (e.target.value.trim() && e.target.value !== conversation.title) patchConversation.mutate({ title: e.target.value.trim() });
              }}
              onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
              className="field h-8 max-w-sm text-[14px]"
            />
          ) : (
            <button onClick={() => setRenaming(true)} className="min-w-0 truncate rounded-md px-2 py-1 text-[14px] font-medium hover:bg-surface-3">
              {conversation.title || "新对话"}
            </button>
          )
        ) : (
          <span className="px-2 text-[14px] font-medium">新对话</span>
        )}
        <div className="ml-auto flex items-center gap-1">
          <ModelPicker
            value={modelRef}
            align="end"
            onChange={(ref) => {
              if (id) patchConversation.mutate({ model: ref });
              else setDraftModel(ref);
              try {
                localStorage.setItem("agentos_chat_model", ref);
              } catch {
                /* ignore */
              }
            }}
          />
          {id && conversation && (
            <IconButton label="对话设置" onClick={() => setSettingsOpen(true)}>
              <SlidersHorizontal className="size-[17px]" />
            </IconButton>
          )}
        </div>
      </header>

      <div
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget;
          setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
        }}
        className="relative min-h-0 flex-1 overflow-y-auto"
      >
        {!id ? (
          <div className="mx-auto flex min-h-full max-w-[760px] flex-col justify-center px-5 py-10">
            <h1 className="text-[28px] leading-tight font-semibold tracking-tight">今天想聊点什么？</h1>
            <p className="mt-2 text-[14px] text-ink-3">
              {modelInfo
                ? `正在使用 ${modelInfo.name}${modelInfo.loaded ? "，已在显存里，可以马上回答" : "，第一次回答需要先加载模型"}`
                : models.isLoading
                  ? "正在读取本地模型…"
                  : "还没有可用的模型，去设备页检查模型服务"}
            </p>
            <div className="mt-7 flex flex-wrap gap-1.5">
              {(presets.data || []).map((preset) => (
                <button
                  key={preset.id}
                  onClick={() => setDraftPreset(preset.id)}
                  className={cn(
                    "h-8 rounded-full border px-3.5 text-[13px] transition-colors",
                    draftPreset === preset.id ? "border-accent bg-accent-soft text-accent-ink" : "border-line text-ink-2 hover:border-line-strong hover:text-ink",
                  )}
                >
                  {preset.name}
                </button>
              ))}
            </div>
            <div className="mt-4">
              <Composer
                key="new"
                busy={false}
                onSend={handleSend}
                onStop={() => undefined}
                think={draftThink}
                onThinkChange={setDraftThink}
                supportsThinking={caps.has("thinking")}
                supportsVision={caps.has("vision")}
                autoFocus
              />
            </div>
            <div className="mt-5 flex flex-col gap-1">
              {(SUGGESTIONS[draftPreset] || SUGGESTIONS.assistant).map((text) => (
                <button key={text} onClick={() => void handleSend(text, [])} className="rounded-[10px] px-3 py-2 text-left text-[13.5px] text-ink-2 hover:bg-surface-2 hover:text-ink">
                  {text}
                </button>
              ))}
            </div>
          </div>
        ) : detail.isLoading ? (
          <div className="flex h-full items-center justify-center">
            <Spinner />
          </div>
        ) : (
          <div className="mx-auto flex max-w-[760px] flex-col gap-7 px-5 pt-8 pb-6">
            {messages.map((message) =>
              message.role === "user" ? (
                <UserMessage
                  key={message.id}
                  message={message}
                  editable={!live}
                  onEdit={(text) => send(id!, { content: text, edit_message_id: message.id }, text)}
                />
              ) : (
                <AssistantMessage
                  key={message.id}
                  message={message}
                  isLast={message.id === lastAssistant?.id}
                  onRegenerate={!live ? () => send(id!, { regenerate: true }) : undefined}
                  live={
                    live && live.assistantId === message.id
                      ? {
                          ...live,
                          thinkingSeconds:
                            live.thinkingStartedAt && live.thinkingEndedAt ? (live.thinkingEndedAt - live.thinkingStartedAt) / 1000 : undefined,
                        }
                      : undefined
                  }
                />
              ),
            )}
            {pendingUser && <UserMessage message={{ id: "pending", content: pendingUser, attachments: [] }} />}
            {live?.userMessage && !messages.some((m) => m.id === live.userMessage!.id) && <UserMessage message={live.userMessage} />}
            {showLiveTail && live && (
              <AssistantMessage
                message={{ id: "live", conversation_id: id, seq: 0, role: "assistant", content: "", thinking: "", attachments: [], model: modelRef, status: "streaming", stats: {}, error: "", created_at: "" }}
                live={{
                  ...live,
                  thinkingSeconds: live.thinkingStartedAt && live.thinkingEndedAt ? (live.thinkingEndedAt - live.thinkingStartedAt) / 1000 : undefined,
                }}
              />
            )}
            {errorBanner && !live && <div className="rounded-[12px] bg-red-soft px-4 py-3 text-[13px] text-red">{errorBanner}</div>}
          </div>
        )}
        {id && !atBottom && (
          <button
            onClick={() => scrollToBottom()}
            className="sticky bottom-3 left-1/2 mx-auto flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-line bg-surface px-3 py-1.5 text-[12.5px] text-ink-2 shadow-[var(--shadow-card)] hover:text-ink"
          >
            <ArrowDown className="size-3.5" />
            回到最新
          </button>
        )}
      </div>

      {id && (
        <div className="shrink-0 px-4 pb-4">
          <div className="mx-auto max-w-[760px]">
            <Composer
              key={id}
              busy={Boolean(live)}
              onSend={handleSend}
              onStop={() => void api(`/v1/chat/conversations/${id}/stop`, { method: "POST" })}
              think={think}
              onThinkChange={(value) => conversation && patchConversation.mutate({ params: { ...conversation.params, think: value } })}
              supportsThinking={caps.has("thinking")}
              supportsVision={caps.has("vision")}
            />
            <div className="mt-2 text-center text-[11.5px] text-ink-3">回答由你的本地模型生成，重要信息请自行核实</div>
          </div>
        </div>
      )}
      {conversation && (
        <SettingsDialog open={settingsOpen} onOpenChange={setSettingsOpen} conversation={conversation} onSave={(body) => patchConversation.mutate(body)} />
      )}
    </div>
  );

  if (phone) {
    if (!showList) return thread;
    return (
      <div className="flex h-full flex-col">
        <div className="flex h-12 items-center justify-between border-b border-line px-4">
          <span className="text-[16px] font-semibold">对话</span>
          <Button
            size="sm"
            variant="primary"
            icon={<Plus className="size-4" />}
            onClick={() => {
              navigate("/chat");
              setShowList(false);
            }}
          >
            新对话
          </Button>
        </div>
        <div className="min-h-0 flex-1">
          <ConversationList activeId={id} onPick={() => setShowList(false)} />
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full">
      {showList && (
        <div className="flex w-[264px] shrink-0 flex-col border-r border-line bg-surface-2/50">
          <ConversationList activeId={id} onHide={() => setShowList(false)} />
        </div>
      )}
      {thread}
    </div>
  );
}
