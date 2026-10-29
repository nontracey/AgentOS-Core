"""Browser-friendly derivatives of immutable media, cached on disk by asset id.

Thumbnails of images and of a video's first second, H.264 previews of node video output
(often VP9/WebM), and audio durations. Derivatives are caches, never Assets: deleting the
cache folder only costs regeneration.
"""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path

from PIL import Image, ImageOps

from agentos.adapters.aigc.compositor import RenderError, probe_duration, run_ffmpeg


def thumbnail(source: Path, cache_dir: Path, key: str, width: int, *, ffmpeg: str = "", video: bool = False) -> Path:
    """JPEG thumbnail of an image, or of a video's first second when ``video`` is set."""
    width = max(64, min(1600, width))
    target = cache_dir / f"{key}-{width}.jpg"
    if target.is_file():
        return target
    cache_dir.mkdir(parents=True, exist_ok=True)
    if video:
        if not ffmpeg:
            raise ValueError("video thumbnails need ffmpeg")
        partial = target.with_suffix(".part.jpg")
        for seek in ("0.5", "0"):
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", seek, "-i", str(source),
                 "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2", "-q:v", "4", str(partial)],
                capture_output=True, timeout=60, check=False)
            if result.returncode == 0 and partial.is_file() and partial.stat().st_size > 0:
                partial.replace(target)
                return target
        raise ValueError("no frame could be read from this video")
    with Image.open(source) as opened:
        image: Image.Image = (ImageOps.exif_transpose(opened) or opened).convert("RGB")
        if image.width > width:
            image = image.resize((width, max(1, round(image.height * width / image.width))),
                                 Image.Resampling.LANCZOS)
        partial = target.with_suffix(".part")
        image.save(partial, "JPEG", quality=86, optimize=True, progressive=True)
        partial.replace(target)
    return target


def h264_preview(source: Path, cache_dir: Path, key: str, ffmpeg: str) -> Path:
    """Silent H.264 MP4 that every browser can play, with fast start."""
    target = cache_dir / f"{key}-preview.mp4"
    if target.is_file():
        return target
    cache_dir.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part.mp4")
    run_ffmpeg(ffmpeg, ["-i", str(source), "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(partial)], timeout=900)
    partial.replace(target)
    return target


def media_duration(source: Path, ffmpeg: str = "") -> float:
    """Seconds of audio or video; WAV is read directly, other formats need ffmpeg."""
    try:
        with wave.open(str(source), "rb") as handle:
            return handle.getnframes() / float(handle.getframerate() or 1)
    except (wave.Error, EOFError, OSError):
        pass
    if not ffmpeg:
        return 0.0
    try:
        return probe_duration(ffmpeg, source)
    except RenderError:
        return 0.0
