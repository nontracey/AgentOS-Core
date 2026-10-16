"""Real FFmpeg audio length after a native clip and a processed lip clip join."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from agentos.adapters.aigc import media


class NativeFrameClockTests(unittest.TestCase):
    def test_audio_uses_planned_frame_clock_when_container_is_one_ms_short(self):
        ffmpeg = os.environ.get('AGENTOS_FFMPEG') or shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('Real FFmpeg required')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clips = []
            for name, rate, frames in [('native', 16, 32), ('processed-lip', 30, 30)]:
                target = root / (name + '.mp4')
                subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    f'testsrc2=size=256x256:rate={rate}', '-frames:v', str(frames),
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(target)], check=True, capture_output=True)
                clips.append(target)
            voice = root / 'synthetic-tone.wav'
            subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                'sine=frequency=440:sample_rate=48000:duration=0.9', '-c:a', 'pcm_s16le', str(voice)],
                check=True, capture_output=True)
            actual_probe = media.probe_duration

            def observed_container_rounding(executable, path):
                value = actual_probe(executable, path)
                return value - .001 if path.name == 'joined.mp4' else value

            # Inject the actual observed format-duration discrepancy, keeping all
            # codecs, frame retiming, concatenation and audio mixing real.
            with patch.object(media, 'probe_duration', side_effect=observed_container_rounding):
                seconds = media.compose(ffmpeg, clips, root, root / 'film.mp4', width=256, height=256,
                    fps=30, edits=[{'start': 0, 'duration': 1, 'retime_native': True}] * 2,
                    dialogue=[voice], timing=[{'start': 1.05, 'end': 1.95}])
            self.assertEqual(seconds, 2)
            with wave.open(str(root / 'dialogue-master.wav')) as audio:
                self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getnframes()),
                                 (48000, 2, 96000))
            ffprobe = Path(ffmpeg).with_name('ffprobe.exe' if Path(ffmpeg).suffix == '.exe' else 'ffprobe')
            video = json.loads(subprocess.run([str(ffprobe), '-v', 'error', '-select_streams', 'v:0',
                '-count_frames', '-show_entries', 'stream=nb_read_frames', '-of', 'json', str(root / 'film.mp4')],
                check=True, capture_output=True).stdout)
            self.assertEqual(video['streams'][0]['nb_read_frames'], '60')


if __name__ == '__main__':
    unittest.main()
