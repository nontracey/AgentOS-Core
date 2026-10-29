/* Same-origin API client. Session keys are shared with the legacy UI, so signing in
   once works for both. Access tokens refresh automatically on 401. */

const ACCESS = "agentos_access";
const REFRESH_PERSIST = "agentos_refresh";
const REFRESH_SESSION = "agentos_refresh_session";
const KIND = "agentos_session_kind";
const STATIC = "agentos_token";
const EPOCH = "agentos_session_epoch";

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function safe<T>(fn: () => T, fallback: T): T {
  try {
    return fn();
  } catch {
    return fallback;
  }
}

export function accessToken(): string {
  return safe(() => sessionStorage.getItem(ACCESS) || sessionStorage.getItem(STATIC) || "", "");
}

function refreshToken(): string {
  return safe(() => {
    const temporary = sessionStorage.getItem(REFRESH_SESSION);
    if (sessionStorage.getItem(KIND) === "guest") return temporary || "";
    return temporary || localStorage.getItem(REFRESH_PERSIST) || "";
  }, "");
}

export function hasSession(): boolean {
  return Boolean(accessToken() || refreshToken());
}

export function storeSession(access: string, refresh: string, remember: boolean): void {
  safe(() => {
    if (!sessionStorage.getItem(EPOCH)) sessionStorage.setItem(EPOCH, crypto.randomUUID());
    sessionStorage.setItem(KIND, "admin");
    sessionStorage.setItem(ACCESS, access);
    if (remember) {
      localStorage.setItem(REFRESH_PERSIST, refresh);
      sessionStorage.removeItem(REFRESH_SESSION);
    } else {
      sessionStorage.setItem(REFRESH_SESSION, refresh);
      localStorage.removeItem(REFRESH_PERSIST);
    }
  }, undefined);
}

export function storeStaticToken(token: string): void {
  safe(() => {
    sessionStorage.setItem(EPOCH, crypto.randomUUID());
    sessionStorage.setItem(KIND, "admin");
    sessionStorage.setItem(STATIC, token);
  }, undefined);
}

export function clearSession(): void {
  safe(() => {
    [EPOCH, ACCESS, REFRESH_SESSION, STATIC].forEach((k) => sessionStorage.removeItem(k));
    localStorage.removeItem(REFRESH_PERSIST);
  }, undefined);
}

let refreshing: Promise<boolean> | null = null;

async function refreshSession(): Promise<boolean> {
  const token = refreshToken();
  if (!token) return false;
  refreshing ??= (async () => {
    try {
      const resp = await fetch("/v1/auth/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: token }),
      });
      if (!resp.ok) return false;
      const body = await resp.json();
      const persistent = safe(() => Boolean(localStorage.getItem(REFRESH_PERSIST)), false);
      storeSession(body.access_token, body.refresh_token, persistent);
      return true;
    } catch {
      return false;
    } finally {
      setTimeout(() => (refreshing = null), 0);
    }
  })();
  return refreshing;
}

export const sessionEvents = new EventTarget();

async function errorFrom(resp: Response): Promise<ApiError> {
  let code = `http_${resp.status}`;
  let message = `请求失败（HTTP ${resp.status}）`;
  try {
    const body = await resp.json();
    const detail = body.detail ?? body;
    if (typeof detail === "object" && detail) {
      code = detail.code || code;
      message = detail.message || message;
    } else if (typeof detail === "string") message = detail;
  } catch {
    /* not JSON */
  }
  return new ApiError(resp.status, code, message);
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  raw?: boolean;
}

export async function request(path: string, options: RequestOptions = {}, retried = false): Promise<Response> {
  const headers: Record<string, string> = { ...(options.headers || {}) };
  const token = accessToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let body: BodyInit | undefined;
  if (options.body instanceof Blob || options.body instanceof FormData || options.body instanceof ArrayBuffer) {
    body = options.body as BodyInit;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  let resp: Response;
  try {
    resp = await fetch(path, { method: options.method || (body ? "POST" : "GET"), headers, body, signal: options.signal });
  } catch (error) {
    if ((error as Error).name === "AbortError") throw error;
    throw new ApiError(0, "network", "连不上 AgentOS 服务，请确认它在运行");
  }
  if (resp.status === 401 && !retried && (await refreshSession())) {
    return request(path, options, true);
  }
  if (resp.status === 401) {
    clearSession();
    sessionEvents.dispatchEvent(new Event("expired"));
  }
  if (!resp.ok) throw await errorFrom(resp);
  return resp;
}

export async function api<T = unknown>(path: string, options: RequestOptions = {}): Promise<T> {
  const resp = await request(path, options);
  if (resp.status === 204) return undefined as T;
  const type = resp.headers.get("content-type") || "";
  return (type.includes("application/json") ? resp.json() : resp.text()) as Promise<T>;
}

export async function upload<T = unknown>(path: string, file: File | Blob, name?: string): Promise<T> {
  const fileName = name || (file as File).name || "file";
  const resp = await request(path, {
    method: "POST",
    body: file,
    headers: { "Content-Type": file.type || "application/octet-stream", "X-File-Name": encodeURIComponent(fileName) },
  });
  return resp.json() as Promise<T>;
}

/** Read an authenticated `text/event-stream` response (POST or GET) event by event. */
export async function* sse<T = Record<string, unknown>>(
  path: string,
  options: RequestOptions = {},
): AsyncGenerator<T> {
  const resp = await request(path, { ...options, headers: { Accept: "text/event-stream", ...(options.headers || {}) } });
  if (resp.status === 204 || !resp.body) return;
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index: number;
      while ((index = buffer.indexOf("\n\n")) >= 0) {
        const chunk = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        const data = chunk
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (data) yield JSON.parse(data) as T;
      }
    }
  } finally {
    reader.cancel().catch(() => undefined);
  }
}

export async function downloadAsset(id: string, name: string): Promise<void> {
  const resp = await request(`/v1/assets/${encodeURIComponent(id)}/download`);
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}
