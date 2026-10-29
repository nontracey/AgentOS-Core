import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

export function duration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0 秒";
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    const rest = Math.round(seconds % 60);
    return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`;
  }
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
}

/** Animation timing notation: seconds + frames (e.g. 3+12 at 24 fps). */
export function sheetTime(seconds: number, fps = 24): string {
  const whole = Math.floor(seconds);
  const frames = Math.round((seconds - whole) * fps);
  return frames >= fps ? `${whole + 1}+0` : `${whole}+${frames}`;
}

export function timecode(seconds: number): string {
  const s = Math.max(0, seconds);
  const m = Math.floor(s / 60);
  const r = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

export function bytes(size: number): string {
  if (!size) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = size;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

export function relativeTime(iso: string): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "";
  const diff = (Date.now() - then) / 1000;
  if (diff < 45) return "刚刚";
  if (diff < 3600) return `${Math.round(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.round(diff / 86400)} 天前`;
  const date = new Date(then);
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日`;
}

export function dayGroup(iso: string): string {
  const date = new Date(iso);
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = date.getTime();
  if (t >= start) return "今天";
  if (t >= start - 86400_000) return "昨天";
  if (t >= start - 6 * 86400_000) return "最近 7 天";
  if (t >= start - 29 * 86400_000) return "最近 30 天";
  return "更早";
}

export function modelLabel(ref: string): string {
  if (!ref) return "";
  const name = ref.includes("/") ? ref.split("/").slice(1).join("/") : ref;
  return name.replace(/:latest$/, "");
}
