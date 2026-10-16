"""Real FFmpeg regression: sparse dialogue across the12-input batching boundary."""
from __future__ import annotations

import math
import shutil
import wave
from array import array
from pathlib import Path

import pytest

from agentos.adapters.aigc.media import mix_dialogue


def tone(path: Path, frequency: int) -> None:
    data = array('h', (round(6000 * math.sin(2 * math.pi * frequency * i / 48000))
                       for i in range(12000)))
    with wave.open(str(path), 'wb') as out:
        out.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        out.writeframes(data.tobytes())


def window(path: Path, start: float, end: float) -> array:
    with wave.open(str(path), 'rb') as source:
        assert source.getframerate() == 48000 and source.getsampwidth() == 2
        source.setpos(round(start * 48000))
        return array('h', source.readframes(round((end - start) * 48000)))[::source.getnchannels()]


def test_late_twelfth_voice_stays_late_and_thirteenth_survives_next_batch(tmp_path: Path) -> None:
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg or not shutil.which('ffprobe'):
        pytest.skip('Real FFmpeg and FFprobe are required')
    ordinary, twelfth, thirteenth = (tmp_path / x for x in ['ordinary.wav', 'twelfth.wav', 'thirteenth.wav'])
    for path, frequency in [(ordinary, 440), (twelfth, 880), (thirteenth, 1320)]:
        tone(path, frequency)
    starts = [1 + 2 * i for i in range(11)] + [27, 29]
    result = mix_dialogue(ffmpeg, tmp_path, 30, [ordinary] * 11 + [twelfth, thirteenth],
                          [{'start': start, 'end': start + .25} for start in starts])
    assert max(abs(v) for v in window(result, .05, .8)) < 2, 'late voice leaked into the opening gap'
    for start, frequency in [(1, 440), (27, 880), (29, 1320)]:
        data = window(result, start + .05, start + .20)
        assert max(abs(v) for v in data) > 1000, f'missing voice at{start}s'
        crossings = sum(a < 0 <= b for a, b in zip(data, data[1:], strict=False))
        assert abs(crossings / .15 - frequency) < 15
    with wave.open(str(result), 'rb') as source:
        assert source.getnframes() == 30 * 48000
