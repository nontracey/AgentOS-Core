"""Real FFmpeg evidence for full guided endpoints, never user/reference media."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from agentos.adapters.aigc.media import compose, ffmpeg_run, normalize_clip, probe_duration
from agentos.kernel.errors import AgentOSError


@pytest.fixture
def media_tools():
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('Real FFmpeg and FFprobe are required for native endpoint verification')
    return ffmpeg, ffprobe


def moving_native(ffmpeg: str, root: Path, *, frames: int = 33) -> Path:
    images = root / 'synthetic-frames'
    images.mkdir()
    for index in range(frames):
        color = 'red' if index == 0 else 'blue' if index == frames - 1 else '#103010'
        image = Image.new('RGB', (64, 64), color)
        # A moving marker is distinct from the two full-frame endpoint colors.
        x = round(index / (frames - 1) * 48)
        ImageDraw.Draw(image).rectangle((x, 28, x + 12, 40), fill='yellow')
        image.save(images / f'{index:03d}.png')
    output = root / 'synthetic-native.mkv'
    ffmpeg_run(ffmpeg, ['-framerate', '16', '-i', str(images / '%03d.png'),
                       '-c:v', 'ffv1', str(output)])
    return output


def frames_rgb(ffmpeg: str, path: Path) -> list[Image.Image]:
    result = subprocess.run([ffmpeg, '-v', 'error', '-i', str(path), '-f', 'rawvideo',
                             '-pix_fmt', 'rgb24', '-'], check=True, capture_output=True)
    stride = 64 * 64 * 3
    assert len(result.stdout) % stride == 0
    return [Image.frombytes('RGB', (64, 64), result.stdout[i:i + stride])
            for i in range(0, len(result.stdout), stride)]


def assert_red(pixel):
    assert pixel[0] > 220 and pixel[1] < 35 and pixel[2] < 35


def assert_blue(pixel):
    assert pixel[2] > 220 and pixel[0] < 35 and pixel[1] < 35


@pytest.mark.parametrize('native_frames,target_frames', [(33, 30), (33, 90), (81, 7)])
def test_whole_native_retime_keeps_both_endpoints_and_motion(
        media_tools, tmp_path, native_frames, target_frames):
    ffmpeg, _ = media_tools
    source = moving_native(ffmpeg, tmp_path, frames=native_frames)
    output = tmp_path / 'retimed.mp4'
    normalize_clip(ffmpeg, source, output, fps=30, duration=target_frames / 30,
                   retime_native=True)
    decoded = frames_rgb(ffmpeg, output)
    assert len(decoded) == target_frames
    assert_red(decoded[0].getpixel((4, 4)))
    assert_blue(decoded[-1].getpixel((4, 4)))
    # Mid-sequence distinct moving marker positions, not a still/last-frame pad.
    positions = set()
    for frame in decoded[1:-1]:
        xs = [x for x in range(64) if frame.getpixel((x, 32))[0] > 180
              and frame.getpixel((x, 32))[1] > 180 and frame.getpixel((x, 32))[2] < 60]
        if xs:
            positions.add(round(sum(xs) / len(xs)))
    assert len(positions) >= min(4, target_frames - 2)
    assert probe_duration(ffmpeg, output) == pytest.approx(target_frames / 30, abs=1e-6)


def test_default_trim_keeps_legacy_endpoint_loss_until_explicit_opt_in(media_tools, tmp_path):
    ffmpeg, _ = media_tools
    source = moving_native(ffmpeg, tmp_path)
    work = tmp_path / 'legacy'
    work.mkdir()
    output = work / 'trimmed.mp4'
    assert compose(ffmpeg, [source], work, output, width=64, height=64, fps=30,
                   edits=[{'start': 0, 'duration': 1}]) == pytest.approx(1)
    last = frames_rgb(ffmpeg, output)[-1].getpixel((4, 4))
    assert not (last[2] > 220 and last[0] < 35)


def test_lipsync_25fps_retime_quantizes_target_slot_without_losing_endpoints(media_tools, tmp_path):
    ffmpeg, _ = media_tools
    source = moving_native(ffmpeg, tmp_path)
    output = tmp_path / 'lip-input.mp4'
    normalize_clip(ffmpeg, source, output, fps=25, duration=43 / 30, retime_native=True)
    decoded = frames_rgb(ffmpeg, output)
    assert len(decoded) == 36
    assert_red(decoded[0].getpixel((4, 4)))
    assert_blue(decoded[-1].getpixel((4, 4)))
    assert probe_duration(ffmpeg, output) == pytest.approx(36 / 25, abs=1e-6)
    assert abs(36 / 25 - 43 / 30) < .5 / 25


def test_same_target_retime_after_lipsync_does_not_accelerate_timeline(media_tools, tmp_path):
    ffmpeg, _ = media_tools
    source = moving_native(ffmpeg, tmp_path)
    before_worker = tmp_path / 'synthetic-lip-input.mp4'
    normalize_clip(ffmpeg, source, before_worker, fps=30, duration=43 / 30, retime_native=True)
    # Re-encode as a synthetic worker would; this fixture performs no lip model.
    after_worker = tmp_path / 'synthetic-lip-result.mp4'
    ffmpeg_run(ffmpeg, ['-i', str(before_worker), '-an', '-c:v', 'libx264', str(after_worker)])
    work = tmp_path / 'compose'
    work.mkdir()
    final = work / 'film.mp4'
    assert compose(ffmpeg, [after_worker], work, final, width=64, height=64, fps=30,
                   edits=[{'start': 0, 'duration': 43 / 30, 'retime_native': True}]) == pytest.approx(43 / 30)
    before, after = frames_rgb(ffmpeg, before_worker), frames_rgb(ffmpeg, final)
    assert len(before) == len(after) == 43
    assert_red(after[0].getpixel((4, 4)))
    assert_blue(after[-1].getpixel((4, 4)))
    # Moving marker stays on its original target-time frame, including edges.
    for i in [1, 10, 20, 30, 41]:
        def center(frame):
            xs = [x for x in range(64) if frame.getpixel((x, 32))[0] > 180
                  and frame.getpixel((x, 32))[1] > 180 and frame.getpixel((x, 32))[2] < 60]
            return sum(xs) / len(xs)
        assert center(after[i]) == pytest.approx(center(before[i]), abs=1)


@pytest.mark.parametrize('options,expected_input,expected_duration', [
    ({}, [], []),
    ({'duration': 1, 'start': .25}, ['-ss', '0.25'], ['-t', '1']),
    ({'duration': 1, 'still': True}, ['-loop', '1'], ['-t', '1']),
    ({'duration': 1, 'retime_native': False}, ['-ss', '0'], ['-t', '1']),
])
def test_default_normalization_ffmpeg_arguments_are_unchanged(
        tmp_path, monkeypatch, options, expected_input, expected_duration):
    commands = []
    monkeypatch.setattr('agentos.adapters.aigc.media.ffmpeg_run',
                        lambda executable, args: commands.append((executable, args)))
    monkeypatch.setattr('agentos.adapters.aigc.media.probe_duration', lambda *args: 2)
    source, output = tmp_path / 'source.mp4', tmp_path / 'output.mp4'
    normalize_clip('ffmpeg', source, output, width=64, height=64, fps=30, **options)
    assert commands == [('ffmpeg', [*expected_input, '-i', str(source), *expected_duration,
        '-vf', 'scale=64:64:force_original_aspect_ratio=decrease,pad=64:64:(ow-iw)/2:(oh-ih)/2,'
        'fps=30,format=yuv420p', '-an', '-c:v', 'libx264', '-crf', '18', str(output)])]


@pytest.mark.parametrize('options', [
    {'duration': 1, 'start': .1}, {'duration': 1, 'still': True}, {},
    {'duration': 1 / 30}, {'duration': float('nan')}, {'duration': float('inf')},
    {'duration': 1, 'fps': 0}, {'duration': 1, 'fps': True},
])
def test_native_retime_rejects_partial_stills_or_impossible_endpoints(tmp_path, options):
    with pytest.raises(AgentOSError):
        normalize_clip('unused', tmp_path / 'source', tmp_path / 'out', **{'fps': 30, **options},
                       retime_native=True)


def test_retime_uses_video_endpoints_instead_of_longer_container_audio(media_tools, tmp_path):
    ffmpeg, _ = media_tools
    source = moving_native(ffmpeg, tmp_path)
    with_audio = tmp_path / 'long-audio.mkv'
    ffmpeg_run(ffmpeg, ['-i', str(source), '-f', 'lavfi', '-i', 'sine=frequency=440:duration=5',
                       '-map', '0:v', '-map', '1:a', '-c:v', 'copy', '-c:a', 'pcm_s16le', str(with_audio)])
    assert probe_duration(ffmpeg, with_audio) > 4.9
    out = tmp_path / 'retimed.mp4'
    normalize_clip(ffmpeg, with_audio, out, fps=30, duration=1, retime_native=True)
    decoded = frames_rgb(ffmpeg, out)
    assert len(decoded) == 30
    assert_red(decoded[0].getpixel((4, 4)))
    assert_blue(decoded[-1].getpixel((4, 4)))


def test_sixteen_native_clips_compose_exactly_900_frames_with_audio_and_last_endpoint(media_tools, tmp_path):
    ffmpeg, ffprobe = media_tools
    source = moving_native(ffmpeg, tmp_path, frames=17)
    voice = tmp_path / 'synthetic-voice.wav'
    ffmpeg_run(ffmpeg, ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.3', str(voice)])
    # Synthetic frame counts mirror the 16-segment 900-frame schedule only.
    # No reference picture or source audio enters this fixture.
    targets = [43, 29, 37, 60, 35, 87, 52, 24, 85, 51, 39, 40, 50, 110, 151, 7]
    assert sum(targets) == 900
    work = tmp_path / 'compose'
    work.mkdir()
    output = work / 'film.mp4'
    duration = compose(ffmpeg, [source] * 16, work, output, width=64, height=64, fps=30,
                       audio=voice, edits=[{'start': 0, 'duration': n / 30, 'retime_native': True}
                                          for n in targets])
    assert duration == pytest.approx(30, abs=1e-6)
    result = subprocess.run([ffprobe, '-v', 'error', '-count_frames', '-show_streams', '-of', 'json',
                             str(output)], check=True, capture_output=True)
    streams = json.loads(result.stdout)['streams']
    video = next(s for s in streams if s['codec_type'] == 'video')
    audio = next(s for s in streams if s['codec_type'] == 'audio')
    assert video['r_frame_rate'] == '30/1' and int(video['nb_read_frames']) == 900
    assert float(video['duration']) == float(audio['duration']) == pytest.approx(30, abs=1e-6)
    decoded = frames_rgb(ffmpeg, output)
    for index in [0, *[sum(targets[:i]) for i in range(1, 16)]]:
        assert_red(decoded[index].getpixel((4, 4)))
    for index in [sum(targets[:i]) - 1 for i in range(1, 17)]:
        assert_blue(decoded[index].getpixel((4, 4)))
    ffmpeg_run(ffmpeg, ['-i', str(output), '-f', 'null', '-'])
