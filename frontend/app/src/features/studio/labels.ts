import type { Camera, Shot } from "@/lib/types";

export const SIZES: Shot["size"][] = ["远景", "全景", "中景", "近景", "特写"];

export const CAMERAS: { value: Camera; label: string }[] = [
  { value: "push_in", label: "缓推" },
  { value: "pull_out", label: "缓拉" },
  { value: "pan_left", label: "左摇" },
  { value: "pan_right", label: "右摇" },
  { value: "tilt_up", label: "上摇" },
  { value: "tilt_down", label: "下摇" },
  { value: "shake", label: "震动" },
  { value: "static", label: "固定" },
];

export const MOTIONS: { value: Shot["motion"]; label: string; hint: string }[] = [
  { value: "still", label: "静帧运镜", hint: "画面加运镜，不需要视频生成" },
  { value: "i2v", label: "图生视频", hint: "Wan2.2 生成真实动作，较慢" },
  { value: "flf2v", label: "首尾帧", hint: "给定起止画面生成中间动作" },
];

export const TRANSITIONS: { value: Shot["transition"]; label: string }[] = [
  { value: "cut", label: "直切" },
  { value: "fade", label: "黑场" },
  { value: "dissolve", label: "叠化" },
  { value: "flash", label: "闪白" },
];

export const EMOTION_TONES: Record<string, string> = {
  高兴: "text-[#c47a00]",
  愤怒: "text-red",
  悲伤: "text-blue",
  恐惧: "text-[#8250df]",
  反感: "text-[#7a6a2f]",
  低落: "text-ink-3",
  惊讶: "text-[#d35d0e]",
  平静: "text-ink-3",
};

export const CHARACTER_COLORS = ["#d8931c", "#2f7fc1", "#c4466f", "#23875d", "#8250df", "#b35900", "#0f8a8a", "#6b6f7b"];

export function characterColor(index: number): string {
  return CHARACTER_COLORS[index % CHARACTER_COLORS.length];
}

export const FORMAT_ASPECT: Record<string, string> = { "9:16": "9 / 16", "16:9": "16 / 9", "1:1": "1 / 1" };

export const GENRES = ["都市逆袭", "玄幻修仙", "校园青春", "甜宠恋爱", "悬疑推理", "末世求生", "宫斗权谋", "搞笑日常"];

const STAGE_NAMES: Record<string, string> = {
  script: "剧本",
  breakdown: "分镜",
  refs: "定妆",
  voices: "配音",
  frames: "画面",
  end_frames: "尾帧",
  clips: "动态",
  render: "导出",
};

export function jobKindLabel(kind: string): string {
  if (kind.startsWith("stage:")) return `下一步 · ${STAGE_NAMES[kind.slice(6)] || kind.slice(6)}`;
  return (
    {
      frame: "画面",
      end_frame: "尾帧",
      clip: "动态",
      voice: "配音",
      voice_sample: "试音",
      lipsync: "口型",
      char_ref: "定妆",
      loc_ref: "场景图",
      render: "合成",
      "director:script": "剧本",
      "director:breakdown": "分镜",
      "director:prompts": "提示词",
    }[kind] || kind
  );
}
