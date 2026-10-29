/* Live generations keyed by conversation. Streams survive navigation; reopening a
   conversation that is still generating re-attaches to the server-side stream. */
import { useSyncExternalStore } from "react";
import { sse } from "@/lib/api";
import type { ChatMessage, GpuSnapshot } from "@/lib/types";

export interface LiveState {
  conversationId: string;
  assistantId: string;
  userMessage?: ChatMessage;
  pendingText?: string;
  content: string;
  thinking: string;
  status: "connecting" | "waiting_gpu" | "loading" | "thinking" | "generating" | "done" | "error";
  gpu?: GpuSnapshot;
  notes: string[];
  error: string;
  startedAt: number;
  thinkingStartedAt?: number;
  thinkingEndedAt?: number;
  final?: ChatMessage;
}

type Event =
  | { type: "start"; user_message: ChatMessage; assistant_message: ChatMessage }
  | { type: "snapshot"; assistant_message_id: string; content: string; thinking: string; status: string }
  | { type: "status"; status: LiveState["status"]; gpu?: GpuSnapshot }
  | { type: "thinking"; text: string }
  | { type: "delta"; text: string }
  | { type: "notice"; notes: string[] }
  | { type: "done"; message: ChatMessage }
  | { type: "error"; message: string; assistant_message?: ChatMessage }
  | { type: "end" };

const states = new Map<string, LiveState>();
const controllers = new Map<string, AbortController>();
const listeners = new Set<() => void>();
const finishedListeners = new Set<(cid: string) => void>();
let version = 0;

function emit(): void {
  version += 1;
  listeners.forEach((listener) => listener());
}

function patch(cid: string, change: Partial<LiveState>): void {
  const current = states.get(cid);
  if (!current) return;
  states.set(cid, { ...current, ...change });
  emit();
}

function apply(cid: string, event: Event): void {
  const state = states.get(cid);
  if (!state) return;
  switch (event.type) {
    case "start":
      patch(cid, { assistantId: event.assistant_message.id, userMessage: event.user_message, pendingText: undefined });
      break;
    case "snapshot":
      patch(cid, {
        assistantId: event.assistant_message_id,
        content: event.content,
        thinking: event.thinking,
        status: (event.status as LiveState["status"]) || "generating",
      });
      break;
    case "status":
      patch(cid, { status: event.status, gpu: event.gpu || state.gpu });
      break;
    case "thinking":
      patch(cid, {
        thinking: state.thinking + event.text,
        thinkingStartedAt: state.thinkingStartedAt ?? Date.now(),
        status: "thinking",
      });
      break;
    case "delta":
      patch(cid, {
        content: state.content + event.text,
        status: "generating",
        thinkingEndedAt: state.thinking && !state.thinkingEndedAt ? Date.now() : state.thinkingEndedAt,
      });
      break;
    case "notice":
      patch(cid, { notes: [...state.notes, ...event.notes] });
      break;
    case "done":
      patch(cid, { status: "done", final: event.message });
      break;
    case "error":
      patch(cid, { status: "error", error: event.message, final: event.assistant_message });
      break;
    case "end":
      break;
  }
}

async function run(cid: string, path: string, init: { method?: string; body?: unknown }): Promise<void> {
  const controller = new AbortController();
  controllers.set(cid, controller);
  try {
    for await (const event of sse<Event>(path, { ...init, signal: controller.signal })) {
      apply(cid, event);
      if (event.type === "end") break;
    }
  } catch (error) {
    if ((error as Error).name !== "AbortError") {
      patch(cid, { status: "error", error: (error as Error).message || "连接中断" });
    }
  } finally {
    controllers.delete(cid);
    const state = states.get(cid);
    states.delete(cid);
    emit();
    finishedListeners.forEach((listener) => listener(cid));
    if (state?.status === "error" && !state.final) {
      // Keep the error visible until the next refresh of the conversation.
      lastErrors.set(cid, state.error);
      emit();
    }
  }
}

export const lastErrors = new Map<string, string>();

export function send(cid: string, body: Record<string, unknown>, pendingText = ""): void {
  if (states.has(cid)) return;
  lastErrors.delete(cid);
  states.set(cid, {
    conversationId: cid,
    assistantId: "",
    pendingText,
    content: "",
    thinking: "",
    status: "connecting",
    notes: [],
    error: "",
    startedAt: Date.now(),
  });
  emit();
  void run(cid, `/v1/chat/conversations/${cid}/messages`, { method: "POST", body });
}

export function attach(cid: string): void {
  if (states.has(cid)) return;
  states.set(cid, {
    conversationId: cid,
    assistantId: "",
    content: "",
    thinking: "",
    status: "connecting",
    notes: [],
    error: "",
    startedAt: Date.now(),
  });
  emit();
  void run(cid, `/v1/chat/conversations/${cid}/live`, {});
}

export function detach(cid: string): void {
  controllers.get(cid)?.abort();
}

export function isLive(cid: string): boolean {
  return states.has(cid);
}

export function onFinished(listener: (cid: string) => void): () => void {
  finishedListeners.add(listener);
  return () => finishedListeners.delete(listener);
}

export function useLive(cid: string | undefined): LiveState | undefined {
  useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => version,
  );
  return cid ? states.get(cid) : undefined;
}

export function useAnyLive(): string[] {
  useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => version,
  );
  return [...states.keys()];
}
