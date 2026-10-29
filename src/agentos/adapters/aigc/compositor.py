"""Local film compositor: timing, camera moves, subtitles, transitions and audio mix.

Runs on the control plane with FFmpeg + Pillow (no GPU). Stills get eased camera moves
rendered with sub-pixel accuracy (no zoompan jitter); clips are fitted to the shot length;
dialogue is placed sample-exactly; music ducks under speech; loudness is normalized.
"""
from __future__ import annotations

import math
import random
import re
import shutil
import subprocess
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SAMPLE_RATE = 48_000
DISSOLVE = 0.45
FADE = 0.3

Progress = Callable[[str, float], None]


class RenderError(Exception):
    pass


def find_ffmpeg(configured: str = "") -> str | None:
    for candidate in (configured, shutil.which("ffmpeg") or "", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                      "/usr/bin/ffmpeg"):
        if candidate and Path(candidate).is_file():
            return candidate
    try:  # optional bundled binary (pip install imageio-ffmpeg)
        import imageio_ffmpeg  # type: ignore[import-not-found]

        path = str(imageio_ffmpeg.get_ffmpeg_exe())
        return path if Path(path).is_file() else None
    except Exception:  # noqa: BLE001 - optional dependency
        return None


def run_ffmpeg(ffmpeg: str, args: list[str], *, stdin: Any = None, timeout: float = 3600) -> None:
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        process = subprocess.run(command, input=stdin, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RenderError("FFmpeg 处理超时") from exc
    if process.returncode:
        detail = process.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise RenderError("FFmpeg 失败：" + " / ".join(detail))


def probe_duration(ffmpeg: str, path: Path) -> float:
    """Container duration from FFmpeg's header (no decode, works without ffprobe)."""
    process = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, timeout=60, check=False)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", process.stderr.decode("utf-8", "replace"))
    if not match:
        raise RenderError(f"无法读取媒体时长：{path.name}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


# ------------------------------------------------------------------ fonts & subtitles
_FONT_CANDIDATES = [
    ("/System/Library/Fonts/PingFang.ttc", ("PingFang SC",), ("Semibold", "Medium", "Regular")),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", ("Hiragino Sans GB",), ("W6", "W3")),
    ("/System/Library/Fonts/STHeiti Medium.ttc", ("Heiti SC",), ("Medium",)),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", ("Noto Sans CJK SC",), ("Bold",)),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc", ("Noto Sans CJK SC",), ("Medium",)),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", ("Noto Sans CJK SC",), ("Regular",)),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", ("WenQuanYi Zen Hei",), ("Regular",)),
    ("C:/Windows/Fonts/msyhbd.ttc", ("Microsoft YaHei",), ("Bold",)),
    ("C:/Windows/Fonts/msyh.ttc", ("Microsoft YaHei",), ("Regular",)),
]
_font_cache: dict[int, ImageFont.FreeTypeFont] = {}
_font_source: tuple[str, int] | None = None


def _locate_font() -> tuple[str, int]:
    global _font_source
    if _font_source is not None:
        return _font_source
    for path, families, styles in _FONT_CANDIDATES:
        if not Path(path).is_file():
            continue
        best: tuple[int, int] | None = None
        for index in range(32):
            try:
                family, style = ImageFont.truetype(path, 12, index=index).getname()
            except OSError:
                break
            if family in families:
                rank = styles.index(style) if style in styles else len(styles)
                if best is None or rank < best[0]:
                    best = (rank, index)
        if best is not None:
            _font_source = (path, best[1])
            return _font_source
    raise RenderError("找不到中文字体（需要 PingFang、Noto Sans CJK 或微软雅黑）")


def font(size: int) -> ImageFont.FreeTypeFont:
    if size not in _font_cache:
        path, index = _locate_font()
        _font_cache[size] = ImageFont.truetype(path, size, index=index)
    return _font_cache[size]


def wrap_text(text: str, face: ImageFont.FreeTypeFont, max_width: float, max_lines: int = 2) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text.strip():
        trial = current + char
        if current and face.getlength(trial) > max_width:
            # Prefer breaking after punctuation within the last few characters.
            cut = max((current.rfind(p) for p in "，。！？、；：,.!?;: "), default=-1)
            if cut >= len(current) - 6 and cut > 0:
                lines.append(current[:cut + 1])
                current = current[cut + 1:] + char
            else:
                lines.append(current)
                current = char
        else:
            current = trial
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        merged = "".join(lines[max_lines - 1:])
        lines = lines[:max_lines - 1] + [merged]
    return lines


def subtitle_image(text: str, width: int, height: int, *, size: float, speaker: str = "") -> Image.Image:
    """Transparent full-width strip: white text, dark outline and soft shadow."""
    px = max(18, int(height * size))
    face = font(px)
    content = (speaker + "：" if speaker else "") + text
    rows = wrap_text(content, face, width * 0.86)
    line_height = int(px * 1.32)
    strip_h = line_height * len(rows) + px
    layer = Image.new("RGBA", (width, strip_h), (0, 0, 0, 0))
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    draw, shadow_draw = ImageDraw.Draw(layer), ImageDraw.Draw(shadow)
    stroke = max(2, round(px * 0.085))
    for row_index, row in enumerate(rows):
        row_w = face.getlength(row)
        x = (width - row_w) / 2
        y = px * 0.5 + row_index * line_height
        shadow_draw.text((x + px * 0.04, y + px * 0.06), row, font=face, fill=(0, 0, 0, 170),
                         stroke_width=stroke, stroke_fill=(0, 0, 0, 170))
        draw.text((x, y), row, font=face, fill=(255, 255, 255, 255), stroke_width=stroke,
                  stroke_fill=(16, 14, 20, 235))
    shadow = shadow.filter(ImageFilter.GaussianBlur(px * 0.12))
    return Image.alpha_composite(shadow, layer)


def write_srt(cues: list[LineCue], path: Path) -> None:
    def stamp(t: float) -> str:
        ms = int(round(t * 1000))
        return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    rows = []
    for index, cue in enumerate([c for c in cues if c.text.strip()], start=1):
        rows.append(f"{index}\n{stamp(cue.start)} --> {stamp(cue.start + cue.duration)}\n{cue.text.strip()}\n")
    path.write_text("\n".join(rows), encoding="utf-8")


# ------------------------------------------------------------------ plan
@dataclass
class LineCue:
    text: str
    speaker: str
    audio: Path | None
    start: float            # absolute time in the film
    duration: float


@dataclass
class ShotCue:
    index: int
    shot_id: str
    start: float
    duration: float
    kind: str                # video | image | card
    source: Path | None
    camera: str = "static"
    transition: str = "cut"  # transition into this shot
    fade_out: bool = False   # next shot fades through black
    card_text: str = ""
    lines: list[LineCue] = field(default_factory=list)
    seed: int = 0


@dataclass
class MusicCueSpec:
    path: Path
    gain: float = 0.35
    start: float = 0.0
    duck: bool = True


@dataclass
class RenderPlan:
    width: int
    height: int
    fps: int
    shots: list[ShotCue]
    music: list[MusicCueSpec] = field(default_factory=list)
    subtitles: bool = True
    subtitle_size: float = 0.042
    subtitle_position: float = 0.8
    subtitle_speaker: bool = False
    smooth_motion: bool = False
    crf: int = 18
    preset: str = "medium"

    @property
    def total(self) -> float:
        if not self.shots:
            return 0.0
        last = self.shots[-1]
        return round(last.start + last.duration, 3)

    def lines(self) -> list[LineCue]:
        return [line for shot in self.shots for line in shot.lines]


def layout(shots: list[dict[str, Any]]) -> list[ShotCue]:
    """Place lines inside shots and shots on the film timeline.

    Each shot dict: id, duration, kind, source, camera, transition, card_text, seed and
    lines [{text, speaker, audio, duration, pause_after}]. Shots stretch to fit speech.
    """
    cues: list[ShotCue] = []
    cursor_film = 0.0
    for index, raw in enumerate(shots):
        transition = str(raw.get("transition") or "cut")
        if index == 0 and transition == "dissolve":
            transition = "fade"
        start = cursor_film - (DISSOLVE if transition == "dissolve" else 0.0)
        head = 0.25 if raw.get("lines") else 0.0
        inner = head
        lines: list[LineCue] = []
        for line in raw.get("lines") or []:
            duration = max(0.4, float(line.get("duration") or 0))
            lines.append(LineCue(text=str(line.get("text") or ""), speaker=str(line.get("speaker") or ""),
                                 audio=line.get("audio"), start=round(start + inner, 3), duration=round(duration, 3)))
            inner += duration + float(line.get("pause_after", 0.25))
        needed = inner + (0.35 if lines else 0.0)
        duration = round(max(float(raw.get("duration") or 3.0), needed, 0.8), 3)
        cues.append(ShotCue(index=index + 1, shot_id=str(raw.get("id") or index), start=round(start, 3),
                            duration=duration, kind=str(raw.get("kind") or "card"), source=raw.get("source"),
                            camera=str(raw.get("camera") or "static"), transition=transition,
                            card_text=str(raw.get("card_text") or ""), lines=lines, seed=int(raw.get("seed") or 0)))
        cursor_film = start + duration
    for current, following in zip(cues, cues[1:], strict=False):
        current.fade_out = following.transition == "fade"
    return cues


# ------------------------------------------------------------------ camera moves
def _ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def camera_path(camera: str, t: float, duration: float, seed: int) -> tuple[float, float, float]:
    """(zoom, center_x, center_y) at normalized time t; centers are fractions of the source."""
    e = _ease(t)
    if camera == "push_in":
        return 1.0 + 0.14 * e, 0.5, 0.5 - 0.04 * e
    if camera == "pull_out":
        return 1.14 - 0.14 * e, 0.5, 0.46 + 0.04 * e
    if camera == "pan_left":
        return 1.12, 0.555 - 0.11 * e, 0.5
    if camera == "pan_right":
        return 1.12, 0.445 + 0.11 * e, 0.5
    if camera == "tilt_up":
        return 1.12, 0.5, 0.555 - 0.11 * e
    if camera == "tilt_down":
        return 1.12, 0.5, 0.445 + 0.11 * e
    if camera == "shake":
        seconds = t * duration
        amp = 0.012 * math.exp(-seconds / 0.45) + 0.0015
        rng = random.Random(seed * 1000 + int(seconds * 60))
        return 1.08, 0.5 + rng.uniform(-amp, amp), 0.5 + rng.uniform(-amp, amp)
    return 1.0 + 0.035 * e, 0.5, 0.5  # static: slow breathing push so stills never look frozen


def render_still_frames(source: Path, shot: ShotCue, width: int, height: int, fps: int,
                        subtitles: list[tuple[float, float, Image.Image, int]]) -> Any:
    """Yield RGB frame bytes for a still with an eased camera move, fades and subtitles."""
    with Image.open(source) as opened:
        image = opened.convert("RGB")
    cover = max(width / image.width, height / image.height)
    max_zoom = 1.16
    base = image.resize((max(width, round(image.width * cover * max_zoom)),
                         max(height, round(image.height * cover * max_zoom))), Image.Resampling.LANCZOS)
    if cover * max_zoom > 1.25:  # upscaled noticeably: restore some edge crispness
        base = base.filter(ImageFilter.UnsharpMask(radius=1.6, percent=55, threshold=2))
    scale = base.width / image.width  # source px → base px
    total = max(1, round(shot.duration * fps))
    for frame_index in range(total):
        t = frame_index / max(1, total - 1)
        zoom, cx, cy = camera_path(shot.camera, t, shot.duration, shot.seed or shot.index)
        factor = cover * zoom / scale            # base px → output px
        window_w, window_h = width / factor, height / factor
        center_x = min(max(cx * base.width, window_w / 2), base.width - window_w / 2)
        center_y = min(max(cy * base.height, window_h / 2), base.height - window_h / 2)
        inv = 1 / factor
        frame = base.transform((width, height), Image.Transform.AFFINE,
                               (inv, 0, center_x - width / 2 * inv, 0, inv, center_y - height / 2 * inv),
                               resample=Image.Resampling.BICUBIC)
        seconds = frame_index / fps
        for start, end, overlay, top in subtitles:
            if start <= seconds < end:
                frame.paste(overlay, (0, top), overlay)
        frame = _apply_fades(frame, shot, seconds)
        yield frame.tobytes()


def _apply_fades(frame: Image.Image, shot: ShotCue, seconds: float) -> Image.Image:
    level: float | None = None
    color = (0, 0, 0)
    if shot.transition in ("fade", "flash") and seconds < FADE:
        level = seconds / FADE
        color = (255, 255, 255) if shot.transition == "flash" else (0, 0, 0)
    elif shot.fade_out and seconds > shot.duration - FADE:
        level = max(0.0, (shot.duration - seconds) / FADE)
    if level is None or level >= 1:
        return frame
    return Image.blend(Image.new("RGB", frame.size, color), frame, max(0.0, level))


def card_image(shot: ShotCue, width: int, height: int) -> Image.Image:
    """Placeholder for a cut without a picture yet (shows in previews, never silently in finals)."""
    image = Image.new("RGB", (width, height), (24, 27, 34))
    draw = ImageDraw.Draw(image)
    big = font(max(24, height // 9))
    small = font(max(14, height // 34))
    draw.text((width * 0.08, height * 0.32), f"C{shot.index:02d}", font=big, fill=(240, 177, 63))
    rows = wrap_text(shot.card_text or "画面待生成", small, width * 0.84, max_lines=6)
    for row_index, row in enumerate(rows):
        draw.text((width * 0.08, height * 0.32 + big.size * 1.3 + row_index * small.size * 1.5), row, font=small,
                  fill=(205, 210, 220))
    return image


# ------------------------------------------------------------------ render
def render(plan: RenderPlan, ffmpeg: str, workdir: Path, output: Path, *,
           progress: Progress | None = None) -> dict[str, Any]:
    if not plan.shots:
        raise RenderError("还没有镜头")
    workdir.mkdir(parents=True, exist_ok=True)
    report = progress or (lambda _stage, _fraction: None)
    width, height, fps = plan.width, plan.height, plan.fps
    segments: list[Path] = []
    for position, shot in enumerate(plan.shots):
        report(f"渲染第 {shot.index} 镜", position / (len(plan.shots) + 2))
        segment = workdir / f"seg-{shot.index:03d}.mp4"
        subtitles = _subtitle_layers(plan, shot)
        if shot.kind == "video" and shot.source is not None:
            _render_video_segment(plan, shot, ffmpeg, segment, subtitles, workdir)
        else:
            source = shot.source if shot.kind == "image" and shot.source is not None else None
            if source is None:
                source = workdir / f"card-{shot.index:03d}.png"
                card_image(shot, width, height).save(source)
            frames = render_still_frames(source, shot, width, height, fps,
                                         [(s, e, img, top) for s, e, img, top in subtitles])
            _encode_frames(ffmpeg, frames, width, height, fps, segment, plan)
        segments.append(segment)
    report("拼接镜头", len(plan.shots) / (len(plan.shots) + 2))
    video = workdir / "video.mp4"
    _join(plan, ffmpeg, segments, video)
    report("混音", (len(plan.shots) + 0.5) / (len(plan.shots) + 2))
    audio = _mix_audio(plan, ffmpeg, workdir)
    report("封装成片", (len(plan.shots) + 1) / (len(plan.shots) + 2))
    run_ffmpeg(ffmpeg, ["-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "192k", "-t", f"{plan.total:.3f}", "-movflags", "+faststart",
                        str(output)])
    srt = output.with_suffix(".srt")
    write_srt(plan.lines(), srt)
    poster = output.with_suffix(".jpg")
    run_ffmpeg(ffmpeg, ["-ss", f"{min(1.0, plan.total / 3):.2f}", "-i", str(output), "-frames:v", "1",
                        "-vf", f"scale={min(720, width)}:-2", "-q:v", "3", str(poster)])
    report("完成", 1.0)
    return {"duration": plan.total, "width": width, "height": height, "fps": fps, "srt": srt, "poster": poster,
            "shots": len(plan.shots)}


def _subtitle_layers(plan: RenderPlan, shot: ShotCue) -> list[tuple[float, float, Image.Image, int]]:
    if not plan.subtitles:
        return []
    layers = []
    for line in shot.lines:
        if not line.text.strip():
            continue
        image = subtitle_image(line.text, plan.width, plan.height, size=plan.subtitle_size,
                               speaker=line.speaker if plan.subtitle_speaker else "")
        top = int(plan.height * plan.subtitle_position - image.height / 2)
        start = line.start - shot.start
        layers.append((start, start + line.duration + 0.15, image, max(0, top)))
    return layers


def _encode_frames(ffmpeg: str, frames: Any, width: int, height: int, fps: int, output: Path,
                   plan: RenderPlan) -> None:
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{width}x{height}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", plan.preset,
               "-crf", str(plan.crf), "-pix_fmt", "yuv420p", "-r", str(fps), str(output)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdin is not None
    try:
        for data in frames:
            process.stdin.write(data)
        process.stdin.close()
    except BrokenPipeError:
        pass
    stderr = process.stderr.read() if process.stderr else b""
    if process.wait():
        raise RenderError("FFmpeg 编码失败：" + stderr.decode("utf-8", "replace")[-300:])


def _render_video_segment(plan: RenderPlan, shot: ShotCue, ffmpeg: str, output: Path,
                          subtitles: list[tuple[float, float, Image.Image, int]], workdir: Path) -> None:
    assert shot.source is not None
    width, height, fps = plan.width, plan.height, plan.fps
    clip_seconds = probe_duration(ffmpeg, shot.source)
    stretch = 1.0
    if 0 < clip_seconds < shot.duration:
        stretch = min(1.25, shot.duration / clip_seconds)
    chain = []
    if plan.smooth_motion:
        chain.append(f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
    if stretch > 1.001:
        chain.append(f"setpts={stretch:.4f}*PTS")
    chain += [f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos",
              f"crop={width}:{height}", "setsar=1", f"fps={fps}", "format=yuv420p",
              f"tpad=stop_mode=clone:stop_duration={shot.duration:.3f}", f"trim=duration={shot.duration:.3f}",
              "setpts=PTS-STARTPTS"]
    if shot.transition in ("fade", "flash"):
        chain.append(f"fade=t=in:st=0:d={FADE}:color={'white' if shot.transition == 'flash' else 'black'}")
    if shot.fade_out:
        chain.append(f"fade=t=out:st={max(0.0, shot.duration - FADE):.3f}:d={FADE}")
    inputs = ["-i", str(shot.source)]
    graph = "[0:v]" + ",".join(chain) + "[base]"
    current = "base"
    for index, (start, end, image, top) in enumerate(subtitles, start=1):
        path = workdir / f"sub-{shot.index:03d}-{index}.png"
        image.save(path)
        inputs += ["-loop", "1", "-t", f"{shot.duration:.3f}", "-i", str(path)]
        graph += (f";[{current}][{index}:v]overlay=0:{top}:enable='between(t,{start:.3f},{end:.3f})'"
                  f":eof_action=pass[v{index}]")
        current = f"v{index}"
    run_ffmpeg(ffmpeg, [*inputs, "-filter_complex", graph, "-map", f"[{current}]", "-an", "-c:v", "libx264",
                        "-preset", plan.preset, "-crf", str(plan.crf), "-pix_fmt", "yuv420p", "-r", str(fps),
                        "-t", f"{shot.duration:.3f}", str(output)])


def _join(plan: RenderPlan, ffmpeg: str, segments: list[Path], output: Path) -> None:
    if not any(shot.transition == "dissolve" for shot in plan.shots):
        listing = output.with_suffix(".txt")
        listing.write_text("".join(f"file '{segment.resolve()}'\n" for segment in segments), encoding="utf-8")
        run_ffmpeg(ffmpeg, ["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(output)])
        return
    inputs: list[str] = []
    graph: list[str] = []
    for index, segment in enumerate(segments):
        inputs += ["-i", str(segment)]
        # xfade/concat need identical timebase, rate, format and SAR on both inputs.
        graph.append(f"[{index}:v]setpts=PTS-STARTPTS,fps={plan.fps},format=yuv420p,setsar=1,settb=AVTB[s{index}]")
    current = "s0"
    for index, shot in enumerate(plan.shots[1:], start=1):
        label = f"j{index}"
        if shot.transition == "dissolve":
            graph.append(f"[{current}][s{index}]xfade=transition=fade:duration={DISSOLVE}:"
                         f"offset={shot.start:.3f},settb=AVTB[{label}]")
        else:
            graph.append(f"[{current}][s{index}]concat=n=2:v=1:a=0,settb=AVTB[{label}]")
        current = label
    run_ffmpeg(ffmpeg, [*inputs, "-filter_complex", ";".join(graph), "-map", f"[{current}]", "-c:v", "libx264",
                        "-preset", plan.preset, "-crf", str(plan.crf), "-pix_fmt", "yuv420p", "-r", str(plan.fps),
                        str(output)])


def _pcm(ffmpeg: str, source: Path, target: Path) -> bytes:
    run_ffmpeg(ffmpeg, ["-i", str(source), "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", str(target)])
    return target.read_bytes()


def _mix_audio(plan: RenderPlan, ffmpeg: str, workdir: Path) -> Path:
    total_samples = int(math.ceil(plan.total * SAMPLE_RATE))
    track = bytearray(total_samples * 2)
    for index, line in enumerate(plan.lines()):
        if line.audio is None or not Path(line.audio).is_file():
            continue
        data = _pcm(ffmpeg, Path(line.audio), workdir / f"line-{index:03d}.pcm")
        offset = int(round(line.start * SAMPLE_RATE)) * 2
        data = data[:max(0, len(track) - offset)]
        track[offset:offset + len(data)] = data
    dialogue = workdir / "dialogue.wav"
    with wave.open(str(dialogue), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(track))
    mixed = workdir / "mix.wav"
    loudnorm = "loudnorm=I=-16:TP=-1.5:LRA=11"
    music = [cue for cue in plan.music if Path(cue.path).is_file()]
    if not music:
        run_ffmpeg(ffmpeg, ["-i", str(dialogue), "-af", f"{loudnorm},aresample={SAMPLE_RATE}", "-ac", "2",
                            "-t", f"{plan.total:.3f}", str(mixed)])
        return mixed
    inputs = ["-i", str(dialogue)]
    graph = []
    labels = []
    for index, cue in enumerate(music, start=1):
        inputs += ["-stream_loop", "-1", "-i", str(cue.path)]
        delay = int(cue.start * 1000)
        length = max(0.1, plan.total - cue.start)
        graph.append(f"[{index}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,"
                     f"atrim=duration={length:.3f},volume={cue.gain:.3f},afade=t=in:d=1.0,"
                     f"afade=t=out:st={max(0.0, length - 2.5):.3f}:d=2.5,adelay={delay}|{delay}[m{index}]")
        labels.append(f"[m{index}]")
    graph.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0[music]")
    graph.append("[0:a]aformat=channel_layouts=stereo,asplit=2[dlg][key]")
    if any(cue.duck for cue in music):
        graph.append("[music][key]sidechaincompress=threshold=0.025:ratio=10:attack=15:release=350:makeup=1[bed]")
    else:
        graph.append("[music]anull[bed];[key]anullsink")
    graph.append(f"[dlg][bed]amix=inputs=2:normalize=0,{loudnorm},aresample={SAMPLE_RATE}[out]")
    run_ffmpeg(ffmpeg, [*inputs, "-filter_complex", ";".join(graph), "-map", "[out]", "-ac", "2",
                        "-t", f"{plan.total:.3f}", str(mixed)])
    return mixed
