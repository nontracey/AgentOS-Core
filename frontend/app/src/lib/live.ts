/* One authenticated event stream for studio jobs, takes and the shared GPU, with reconnect. */
import { hasSession, sse } from "./api";
import type { GpuSnapshot, Job, Take } from "./types";

export type LiveEvent =
  | { type: "hello"; gpu: GpuSnapshot }
  | { type: "gpu"; gpu: GpuSnapshot }
  | { type: "job"; job: Job }
  | { type: "take"; take: Take; selected: boolean }
  | { type: "project"; project_id: string };

type Listener = (event: LiveEvent) => void;

const listeners = new Set<Listener>();
let running = false;
let controller: AbortController | null = null;
export let lastGpu: GpuSnapshot | null = null;

async function loop(): Promise<void> {
  let delay = 1000;
  while (running) {
    controller = new AbortController();
    try {
      if (!hasSession()) throw new Error("no session");
      for await (const event of sse<LiveEvent>("/v1/studio/events", { signal: controller.signal })) {
        delay = 1000;
        if (event.type === "hello" || event.type === "gpu") lastGpu = event.gpu;
        listeners.forEach((listener) => listener(event));
      }
    } catch {
      /* reconnect below */
    }
    if (!running) break;
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 2, 15000);
  }
}

export function subscribeLive(listener: Listener): () => void {
  listeners.add(listener);
  if (!running) {
    running = true;
    void loop();
  }
  return () => {
    listeners.delete(listener);
  };
}

export function stopLive(): void {
  running = false;
  controller?.abort();
}
