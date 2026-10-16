"""FFmpeg-based local media processing; no model process or GPU allocations."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter

from agentos.kernel.errors import AgentOSError, ErrorCode


def asset_file(root: Path, ref: str) -> Path:
    import re
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', ref) or ref in {'.', '..'}:
        raise AgentOSError(ErrorCode.INVALID, 'invalid media reference')
    path = (root / ref).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise AgentOSError(ErrorCode.INVALID, 'media reference unavailable')
    return path


def ffmpeg_run(executable: str, args: list[str], *, timeout: float = 300, cwd: Path | None = None) -> None:
    try:
        result = subprocess.run([executable, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', *args],
                                capture_output=True, timeout=timeout, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentOSError(ErrorCode.UNAVAILABLE, 'media processing unavailable or timed out') from exc
    if result.returncode:
        raise AgentOSError(ErrorCode.INVALID, 'media decoding or encoding failed')


def prepare_image(ffmpeg: str, source: Path, output: Path, width: int, height: int) -> None:
    # Composite transparent references on neutral gray, preserving the whole character.
    with Image.open(source) as image:
        rgba = image.convert('RGBA')
        rgba.thumbnail((width, height), Image.Resampling.LANCZOS)
        canvas = Image.new('RGBA', (width, height), (128, 128, 128, 255))
        canvas.alpha_composite(rgba, ((width - rgba.width) // 2, (height - rgba.height) // 2))
        canvas.convert('RGB').save(output)


def prepare_reference(source: Path, output: Path, width: int, height: int, *, max_side: int | None = None) -> None:
    """Preserve reference aspect and detail for upstream style-transfer conditioning."""
    with Image.open(source) as image:
        rgba = image.convert('RGBA')
        scale = (width * height / (rgba.width * rgba.height)) ** .5
        if max_side is not None:
            scale = min(scale, max_side / max(rgba.size))
        size = (max(16, round(rgba.width * scale / 16) * 16),
                max(16, round(rgba.height * scale / 16) * 16))
        rgba = rgba.resize(size, Image.Resampling.LANCZOS)
        canvas = Image.new('RGBA', size, (128, 128, 128, 255))
        canvas.alpha_composite(rgba)
        canvas.convert('RGB').save(output)


def pack_alpha(ffmpeg: str, archive: Path, work: Path, fps: int, output: Path) -> dict[str, Any]:
    frames = work / 'frames'
    frames.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        names = sorted(z.namelist())
        if not names or len(names) > 81 or z.infolist()[0].is_dir():
            raise AgentOSError(ErrorCode.INVALID, 'invalid alpha frame archive')
        if sum(i.file_size for i in z.infolist()) > 512 * 1024**2:
            raise AgentOSError(ErrorCode.INVALID, 'alpha archive exceeds size limit')
        for index, name in enumerate(names):
            if not name.endswith('.png') or '/' in name or '\\' in name:
                raise AgentOSError(ErrorCode.INVALID, 'alpha archive must contain flat PNG frames')
            (frames / f'{index:04d}.png').write_bytes(z.read(name))
    geometry: tuple[int, int] | None = None
    union: Image.Image | None = None
    for index in range(len(names)):
        path = frames / f'{index:04d}.png'
        with Image.open(path) as original:
            if max(original.size) > 1024 or min(original.size) < 16 or any(d % 2 for d in original.size):
                raise AgentOSError(ErrorCode.INVALID, 'unsupported alpha frame geometry')
            rgba = original.convert('RGBA')
        if geometry is None:
            geometry = rgba.size
            if max(geometry) > 1024 or min(geometry) < 16 or any(d % 2 for d in geometry):
                raise AgentOSError(ErrorCode.INVALID, 'unsupported alpha frame geometry')
            union = Image.new('L', geometry)
        elif rgba.size != geometry:
            raise AgentOSError(ErrorCode.INVALID, 'alpha frames have inconsistent geometry')
        alpha = rgba.getchannel('A').point(lambda a: 0 if a < 8 else a)
        core = alpha.point(lambda a: 255 if a > 153 else 0)
        near = core.filter(ImageFilter.MaxFilter(7))
        outer = core.filter(ImageFilter.MaxFilter(9)).point(lambda a: a // 2)
        alpha = ImageChops.multiply(alpha, ImageChops.lighter(near, outer))
        if not alpha.getbbox():
            raise AgentOSError(ErrorCode.INVALID, 'alpha sequence contains an empty foreground')
        assert union is not None
        union = ImageChops.lighter(union, alpha)
        # Match the existing player's premultiplied color contract, remove neutral-gray fringe.
        fringe = ImageChops.invert(alpha).point(lambda a: round(a * 128 / 255))
        color = Image.merge('RGB', tuple(ImageChops.darker(ImageChops.subtract(channel, fringe), alpha)
                                       for channel in rgba.convert('RGB').split()))
        packed = Image.new('RGB', (rgba.width * 2, rgba.height))
        packed.paste(color)
        packed.paste(Image.merge('RGB', (alpha, alpha, alpha)), (rgba.width, 0))
        packed.save(path)
    # Keep native frames and motion. Optional smoothing belongs to an explicit later recipe.
    ffmpeg_run(ffmpeg, ['-framerate', str(fps), '-i', str(frames / '%04d.png'),
        '-pix_fmt', 'yuv420p', '-c:v', 'libx264', '-crf', '15',
        '-movflags', '+faststart', str(output)])
    assert union is not None and geometry is not None
    bbox = union.point(lambda a: 255 if a > 24 else 0).getbbox()
    assert bbox is not None
    mask = Image.new('RGBA', geometry, (255, 255, 255, 0))
    mask.putalpha(union)
    mask.save(work / 'mask.png')
    return {'frames': len(names), 'fps': fps, 'duration': len(names) / fps,
            'width': geometry[0], 'height': geometry[1], 'bodyHeight': bbox[3] - bbox[1],
            'anchorX': (bbox[0] + bbox[2]) / 2, 'floor': bbox[3],
            'layout': 'premultiplied-rgb-alpha-side-by-side', 'interpolated': False,
            'visual_review': 'pending'}


def normalize_clip(ffmpeg: str, source: Path, output: Path, *, fps: int,
                   width: int | None = None, height: int | None = None,
                   start: float = 0, duration: float | None = None,
                   still: bool = False, retime_native: bool = False) -> None:
    """Opt-in whole-native-span retiming; the default keeps the old trim command.

    A frame's timestamp is its start, not the end of its display interval. Map
    decoded first/last timestamps onto target first/last timestamps, then sample
    at the output FPS. This keeps both endpoint images under compression as well
    as stretch. FPS conversion drops/duplicates frames; it adds no new motion.
    """
    if type(retime_native) is not bool or (width is None) != (height is None):
        raise AgentOSError(ErrorCode.INVALID, 'invalid clip normalization options')
    if retime_native and (still or start != 0 or duration is None):
        raise AgentOSError(ErrorCode.INVALID, 'native retiming requires a full video and target duration')
    input_args = ['-loop', '1'] if still else []
    duration_args: list[str] = []
    if duration is not None:
        if not math.isfinite(start + duration) or not 0 <= start <= 60 or not 0 < duration <= 60:
            raise AgentOSError(ErrorCode.INVALID, 'invalid clip edit timing')
        if still and start:
            raise AgentOSError(ErrorCode.INVALID, 'still frames have no trim offset')
        if not retime_native:
            if not still and start + duration > probe_duration(ffmpeg, source) + 1 / fps:
                raise AgentOSError(ErrorCode.INVALID, 'edit extends beyond source clip; regenerate more native frames')
            if not still:
                input_args += ['-ss', str(start)]
            duration_args = ['-t', str(duration)]
    geometry = (f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
                f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,' if width is not None else '')
    if retime_native:
        if type(fps) is not int or not 1 <= fps <= 30:
            raise AgentOSError(ErrorCode.INVALID, 'invalid native retiming frame rate')
        assert duration is not None
        frames = round(duration * fps)
        if frames < 2:
            raise AgentOSError(ErrorCode.INVALID, 'native retiming needs at least two output frames for both endpoints')
        first, last = probe_video_endpoints(ffmpeg, source)
        # AVTB avoids coarse input time bases losing the target endpoint.
        # Round up keeps subsequent compressed timestamps off frame zero;
        # nearest rounding can replace the first image before emitting it.
        factor = (frames - 1) / fps / (last - first)
        filters = (f'settb=AVTB,setpts=(PTS-STARTPTS)*{factor:.17g},'
                   f'{geometry}fps={fps}:start_time=0:round=up:eof_action=pass,format=yuv420p')
        duration_args = ['-frames:v', str(frames)]
    else:
        filters = f'{geometry}fps={fps},format=yuv420p'
    ffmpeg_run(ffmpeg, [*input_args, '-i', str(source), *duration_args, '-vf', filters,
                       '-an', '-c:v', 'libx264', '-crf', '18', str(output)])


def probe_video_endpoints(ffmpeg: str, path: Path) -> tuple[float, float]:
    """Read decoded video endpoints, independently of container/audio duration."""
    sibling = Path(ffmpeg).with_name('ffprobe.exe' if Path(ffmpeg).suffix.lower() == '.exe' else 'ffprobe')
    executable = str(sibling) if sibling.is_file() else shutil.which('ffprobe')
    if not executable:
        raise AgentOSError(ErrorCode.UNAVAILABLE, 'FFprobe is required for reliable native retiming')
    try:
        result = subprocess.run([executable, '-v', 'error', '-select_streams', 'v:0',
            '-show_frames', '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', str(path)],
            capture_output=True, timeout=30)
        frames = json.loads(result.stdout)['frames']
        timestamps = [float(frame['best_effort_timestamp_time']) for frame in frames]
        if (result.returncode or len(timestamps) < 2 or not all(math.isfinite(t) for t in timestamps)
                or not math.isfinite(timestamps[-1] - timestamps[0])
                or any(b <= a for a, b in zip(timestamps, timestamps[1:], strict=False))):
            raise ValueError('invalid video frame timestamps')
        return timestamps[0], timestamps[-1]
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        raise AgentOSError(ErrorCode.INVALID, 'native video endpoints could not be verified') from exc


def compose(ffmpeg: str, clips: list[Path], work: Path, output: Path,
            *, width: int, height: int, fps: int, audio: Path | None = None,
            subtitles: list[dict[str, Any]] | None = None,
            edits: list[dict[str, Any]] | None = None, still: bool = False,
            dialogue: list[Path] | None = None, timing: list[dict[str, float]] | None = None) -> float:
    if not 1 <= len(clips) <= 30:
        raise AgentOSError(ErrorCode.INVALID, 'composition requires 1–30 clips')
    if (edits is not None and len(edits) != len(clips)) or (still and not edits):
        raise AgentOSError(ErrorCode.INVALID, 'every timeline clip requires matching edit timing')
    normalized = []
    for index, clip in enumerate(clips):
        target = work / f'clip-{index:03d}.mp4'
        start, retime_native = 0., False
        length: float | None = None
        if edits:
            start, length = float(edits[index]['start']), float(edits[index]['duration'])
            retime_native = edits[index].get('retime_native', False)
        normalize_clip(ffmpeg, clip, target, fps=fps, width=width, height=height,
                       start=start, duration=length, still=still, retime_native=retime_native)
        normalized.append(target)
    # Generated basenames avoid user-controlled concat syntax and filter injection.
    listing = work / 'concat.txt'
    listing.write_text(''.join(f"file '{p.name}'\n" for p in normalized))
    joined = work / 'joined.mp4'
    ffmpeg_run(ffmpeg, ['-f', 'concat', '-safe', '1', '-i', str(listing), '-c', 'copy', str(joined)])
    # Whole-native retiming emits an exact integer frame count for every clip.
    # A concatenated MP4's format duration can round down by a millisecond;
    # using that clock would shorten the new PCM mix despite all frames being
    # present. Keep the legacy duration probe for ordinary trim/still requests.
    duration = (sum(round(float(edit['duration']) * fps) for edit in edits) / fps
                if edits and all(edit.get('retime_native') is True for edit in edits)
                else probe_duration(ffmpeg, joined))
    if not 0 < duration <= 3600:
        raise AgentOSError(ErrorCode.INVALID, 'composition exceeds the duration limit')
    if dialogue or timing:
        audio = mix_dialogue(ffmpeg, work, duration, dialogue or [], timing or [], audio)
    if subtitles:
        lines = []
        for index, subtitle in enumerate(subtitles):
            start, end = float(subtitle['start']), float(subtitle['end'])
            if not 0 <= start < end <= duration + 1 / fps:
                raise AgentOSError(ErrorCode.INVALID, 'invalid subtitle timing')
            def stamp(seconds: float) -> str:
                ms = round(seconds * 1000)
                return f'{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}'
            text = str(subtitle['text']).replace('\r', ' ').replace('\n', ' ')[:1000]
            lines.append(f'{index + 1}\n{stamp(start)} --> {stamp(end)}\n{text}\n')
        (work / 'subtitles.srt').write_text('\n'.join(lines), encoding='utf-8')
    args = ['-i', str(joined)]
    if audio:
        args += ['-i', str(audio)]
    if subtitles:
        args += ['-i', str(work / 'subtitles.srt')]
    args += ['-map', '0:v', '-c:v', 'copy']
    if audio:
        args += ['-map', '1:a', '-c:a', 'aac', '-af', 'apad']
    if subtitles:
        args += ['-map', f'{2 if audio else 1}:s', '-c:s', 'mov_text', '-disposition:s:0', '0']
        # Render captions into the video for ordinary browser/mobile playback, while
        # retaining the separate SRT and embedded editable subtitle track.
        style = (f'PlayResX={width},PlayResY={height},FontSize={max(14, round(width * .045))},'
                 'MarginV=24,MarginL=16,MarginR=16,Outline=1')
        args += ['-vf', f"subtitles=filename=subtitles.srt:force_style='{style}'",
                 '-c:v', 'libx264', '-crf', '18']
    # -shortest would truncate the last shot when the subtitle stream ends first.
    ffmpeg_run(ffmpeg, [*args, '-t', str(duration), '-movflags', '+faststart', str(output)], cwd=work)
    return duration


def mix_dialogue(ffmpeg: str, work: Path, duration: float, clips: list[Path],
                 timing: list[dict[str, float]], background: Path | None = None) -> Path:
    if len(clips) != len(timing) or len(clips) > 200:
        raise AgentOSError(ErrorCode.INVALID, 'dialogue media and timing must match')
    stems = []
    for offset in range(0, len(clips), 12):
        placed = []
        batch = clips[offset:offset + 12]
        for index, clip in enumerate(batch):
            cue = timing[offset + index]
            start, end = float(cue['start']), float(cue['end'])
            length = probe_duration(ffmpeg, clip)
            partial = 'source_start' in cue
            if not math.isfinite(start + end) or not 0 <= start < end <= duration + (1e-6 if partial else .04):
                raise AgentOSError(ErrorCode.INVALID, 'speech exceeds its planned slot; revise timing before composing')
            crop = ''
            if partial:
                source_start = cue['source_start']
                if (type(source_start) not in (int, float) or not math.isfinite(source_start) or
                        source_start < 0 or source_start + end - start > length + 1e-6):
                    raise AgentOSError(ErrorCode.INVALID, 'speech source range exceeds its actual media duration')
                crop = f'atrim=start={source_start}:end={source_start + end - start},asetpts=PTS-STARTPTS,'
            elif length > end - start + .04:
                raise AgentOSError(ErrorCode.INVALID, 'speech exceeds its planned slot; revise timing before composing')
            delay = round(start * 1000)
            # Sparse delayed/padded branches were observed at wrong positions
            # in a single complex graph. Equal-length PCM stems make each
            # line's placement explicit before mixing, and independently testable.
            line = work / f'dialogue-voice-{offset + index:03d}.wav'
            filters = (f'{crop}aformat=sample_rates=48000:channel_layouts=stereo,'
                       f'adelay={delay}|{delay},apad,atrim=duration={duration}')
            ffmpeg_run(ffmpeg, ['-i', str(clip), '-af', filters, '-t', str(duration),
                               '-c:a', 'pcm_s16le', str(line)])
            placed.append(line)
        stem = work / f'dialogue-{offset:03d}.wav'
        inputs = [value for line in placed for value in ('-i', str(line))]
        ffmpeg_run(ffmpeg, [*inputs, '-filter_complex',
                           f'amix=inputs={len(batch)}:normalize=0:duration=longest',
                           '-t', str(duration), '-c:a', 'pcm_s16le', str(stem)])
        stems.append(stem)
    if background:
        stems.append(background)
    if not stems:
        raise AgentOSError(ErrorCode.INVALID, 'dialogue track is empty')
    args = [value for stem in stems for value in ('-i', str(stem))]
    output = work / 'dialogue-master.wav'
    ffmpeg_run(ffmpeg, [*args, '-filter_complex',
        f'amix=inputs={len(stems)}:normalize=0:duration=longest,alimiter=limit=0.95,apad',
        '-t', str(duration), '-ar', '48000', '-ac', '2', '-c:a', 'pcm_s16le', str(output)])
    return output


def probe_duration(ffmpeg: str, path: Path) -> float:
    sibling = Path(ffmpeg).with_name('ffprobe.exe' if Path(ffmpeg).suffix.lower() == '.exe' else 'ffprobe')
    executable = str(sibling) if sibling.is_file() else shutil.which('ffprobe')
    if not executable:
        raise AgentOSError(ErrorCode.UNAVAILABLE, 'FFprobe is required for reliable composition timing')
    try:
        result = subprocess.run([executable, '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'json', str(path)], capture_output=True, timeout=30)
        value = float(json.loads(result.stdout)['format']['duration'])
        if result.returncode or not math.isfinite(value):
            raise ValueError('invalid duration')
        return value
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        raise AgentOSError(ErrorCode.INVALID, 'media duration could not be verified') from exc


def register_output(path: Path, assets: Path, role: str) -> dict[str, Any]:
    import mimetypes
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    ref = digest + path.suffix.lower()
    assets.mkdir(parents=True, exist_ok=True)
    target = assets / ref
    if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        target.write_bytes(data)
    return {'asset_ref': ref, 'sha256': digest, 'size': len(data),
            'mime': mimetypes.guess_type(ref)[0] or 'application/octet-stream', 'role': role}


def save_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    tmp.replace(path)
