export interface ModelInfo {
  ref: string;
  endpoint: string;
  name: string;
  family: string;
  parameter_size: string;
  quantization: string;
  size_bytes: number;
  context_length: number;
  capabilities: string[];
  loaded: boolean;
  vram_bytes: number;
}

export interface EndpointInfo {
  id: string;
  label: string;
  kind: "ollama" | "openai";
  host: string;
  gpu_shared: boolean;
  online: boolean;
  error: string;
  egress: "local" | "cloud";
  /** The egress policy refuses this endpoint for the purpose that was asked about. */
  blocked: boolean;
  /** Purposes a cloud endpoint is limited to under local egress; null when not limited. */
  open_for: string[] | null;
}

export interface GpuSnapshot {
  busy: boolean;
  holder: { kind: string; label: string; elapsed_seconds: number; job_id?: string } | null;
  waiting: { kind: string; label: string; waited_seconds: number }[];
}

export interface ChatParams {
  temperature: number;
  top_p: number;
  num_ctx: number;
  think: "auto" | "on" | "off";
  keep_alive: string;
}

export interface Conversation {
  id: string;
  title: string;
  model: string;
  preset: string;
  system_prompt: string;
  params: ChatParams;
  pinned: boolean;
  archived: boolean;
  created_at: string;
  updated_at: string;
  message_count?: number;
  live?: boolean;
}

export interface Attachment {
  asset_id: string;
  name: string;
  mime: string;
  size: number;
  kind: "image" | "text";
}

export interface ChatMessage {
  id: string;
  conversation_id: string;
  seq: number;
  role: "user" | "assistant";
  content: string;
  thinking: string;
  attachments: Attachment[];
  model: string;
  status: "pending" | "streaming" | "complete" | "stopped" | "error" | "interrupted";
  stats: {
    tokens_per_second?: number;
    output_tokens?: number;
    input_tokens?: number;
    first_token_seconds?: number;
    thinking_seconds?: number;
    load_seconds?: number;
    wall_seconds?: number;
  };
  error: string;
  created_at: string;
}

export interface Preset {
  id: string;
  name: string;
  description: string;
  system_prompt: string;
  builtin: boolean;
}

/* ---------------- studio ---------------- */
export type Slot =
  | "frame"
  | "end_frame"
  | "clip"
  | "lipsync"
  | "voice"
  | "char_ref"
  | "voice_ref"
  | "loc_ref"
  | "style_ref"
  | "render"
  | "music";

export interface Voice {
  description: string;
  sample_text: string;
  speed: number;
}

export interface Character {
  id: string;
  name: string;
  role: "主角" | "配角" | "群演" | "旁白";
  description: string;
  visual_prompt: string;
  lora_id: string | null;
  lora_strength: number;
  voice: Voice;
  color: string;
}

export interface Location {
  id: string;
  name: string;
  description: string;
  visual_prompt: string;
}

export interface Line {
  id: string;
  character_id: string | null;
  text: string;
  emotion: string;
  intensity: number;
  pause_after: number;
}

export type Camera = "static" | "push_in" | "pull_out" | "pan_left" | "pan_right" | "tilt_up" | "tilt_down" | "shake";

export interface Shot {
  id: string;
  scene: string;
  location_id: string | null;
  character_ids: string[];
  size: "远景" | "全景" | "中景" | "近景" | "特写";
  camera: Camera;
  action: string;
  image_prompt: string;
  motion_prompt: string;
  lines: Line[];
  duration: number;
  motion: "still" | "i2v" | "flf2v";
  transition: "cut" | "fade" | "dissolve" | "flash";
  lipsync: boolean;
  face_box: number[] | null;
  seed: number | null;
  notes: string;
}

export interface MusicCue {
  id: string;
  asset_id: string;
  name: string;
  gain: number;
  start: number;
  duck: boolean;
}

export interface Project {
  id: string;
  title: string;
  logline: string;
  format: "9:16" | "16:9" | "1:1";
  genre: string;
  premise: string;
  target_seconds: number;
  style_id: string;
  style_prompt: string;
  style_negative: string;
  script: string;
  characters: Character[];
  locations: Location[];
  shots: Shot[];
  music: MusicCue[];
  subtitle: { enabled: boolean; size: number; position: number; speaker: boolean };
  engines: { image: string; video: string; voice: string; lipsync: string; director: string };
  smooth_motion: boolean;
  revision: number;
  updated_at: string;
}

export interface Take {
  id: string;
  project_id: string;
  slot: Slot | "voice_sample";
  target_id: string;
  asset_id: string;
  job_id: string;
  meta: Record<string, any>;
  rating: number;
  origin: "generated" | "upload";
  created_at: string;
  mime: string;
  url: string;
  thumb?: string;
  preview_url?: string;
  poster_url?: string;
  srt_url?: string;
}

/* One node of a studio workflow run; a request (batch) is one run. */
export interface Job {
  id: string;
  run_id: string;
  project_id: string;
  kind: string;
  target_id: string;
  engine: string;
  label: string;
  params: Record<string, any>;
  gpu: boolean;
  state: "queued" | "running" | "approval" | "blocked" | "succeeded" | "failed" | "cancelled" | "skipped";
  progress: { stage?: string; text?: string; fraction?: number; done?: number; total?: number };
  result: Record<string, any>;
  error: string;
  attempt: number;
  estimate_seconds: number;
  created_at: string;
  started_at: string;
  finished_at: string;
  approval_id: string;
  step_id: string;
  batch_size: number;
}

export const ACTIVE_JOB_STATES: Job["state"][] = ["queued", "running", "approval", "blocked"];

export interface StudioProgress {
  shots: number;
  frames: number;
  lines: number;
  voices: number;
  moving: number;
  clips: number;
  characters: number;
  refs: number;
  voice_refs: number;
  locations: number;
  loc_refs: number;
  seconds: number;
  rendered: boolean;
}

export interface ProjectView {
  project: Project;
  selections: Record<string, string>;
  takes: Take[];
  jobs: Job[];
  warnings: { shot_id: string; message: string }[];
  progress: StudioProgress;
  estimate: { seconds: number; parts: Record<string, number> };
}

export interface ProjectSummary {
  id: string;
  title: string;
  logline: string;
  format: Project["format"];
  style_id: string;
  genre: string;
  revision: number;
  created_at: string;
  updated_at: string;
  cover: string;
  progress: StudioProgress;
  active_jobs: number;
}

export interface EngineInfo {
  id: string;
  kind: "image" | "video" | "voice" | "lipsync";
  label: string;
  note: string;
  tool: string;
  recipe: string;
  max_refs: number;
  min_refs: number;
  end_frame: boolean;
  lora: boolean;
  available: boolean;
  reason: string;
  estimate_seconds: number;
}

export interface StyleInfo {
  id: string;
  name: string;
  description: string;
  prompt: string;
  negative: string;
}

export interface ToolGrant {
  id: string;
  tool_name: string;
  risks: string[];
  expires_at: string;
  issued_by: string;
  revoked: boolean;
  active: boolean;
}

export interface StudioOverview {
  engines: EngineInfo[];
  styles: StyleInfo[];
  emotions: string[];
  node: { id: string; state: "online" | "offline" | "unpaired" | "unconfigured"; os: string; last_seen: string; tools: string[] };
  workflow: { available: boolean; engine: string; message: string };
  permissions: { tools: { tool: string; label: string; grant: ToolGrant | null }[]; needs_approval: boolean; can_grant: boolean };
  ffmpeg: boolean;
  queue: { queued: number; running: Job[]; waiting: number; eta_seconds: number; gpu: GpuSnapshot };
}
