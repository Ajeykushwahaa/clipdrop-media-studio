import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from media import AUDIO_FORMATS, OUTPUT_FORMATS, convert_media
try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    import shutil
    FFMPEG = shutil.which('ffmpeg')

@unittest.skipUnless(FFMPEG, 'FFmpeg required')
class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'fixture.mkv'
        subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-nostdin', '-f', 'lavfi', '-i', 'testsrc=size=64x48:rate=10', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '0.3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(self.source)], check=True, capture_output=True)
    def tearDown(self):
        self.temp.cleanup()
    def test_all_nine_formats_decode(self):
        for fmt in OUTPUT_FORMATS:
            with self.subTest(format=fmt):
                target = self.root / ('result.' + fmt)
                progress = []
                convert_media(self.source, target, fmt, FFMPEG, progress.append)
                self.assertGreater(target.stat().st_size, 0)
                self.assertEqual(progress[-1], 100)
                result = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-nostdin', '-i', str(target), '-map', '0:a:0' if fmt in AUDIO_FORMATS else '0:v:0', '-f', 'null', '-'], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
    def test_missing_tracks(self):
        audio = self.root / 'audio.wav'
        convert_media(self.source, audio, 'wav', FFMPEG)
        with self.assertRaisesRegex(RuntimeError, 'no usable video'):
            convert_media(audio, self.root / 'out.mp4', 'mp4', FFMPEG)
        video = self.root / 'silent.mkv'
        subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-i', str(self.source), '-an', '-c:v', 'copy', str(video)], check=True, capture_output=True)
        with self.assertRaisesRegex(RuntimeError, 'no usable audio'):
            convert_media(video, self.root / 'out.mp3', 'mp3', FFMPEG)
    def test_failure_preserves_target(self):
        broken = self.root / 'broken.dat'
        broken.write_bytes(b'not media')
        target = self.root / 'existing.mp4'
        target.write_bytes(b'original')
        with self.assertRaisesRegex(RuntimeError, 'could not convert'):
            convert_media(broken, target, 'mp4', FFMPEG)
        self.assertEqual(target.read_bytes(), b'original')
        self.assertEqual(list(self.root.glob('.convert-*')), [])
    def test_timeout_cleanup(self):
        with patch('media.CONVERSION_TIMEOUT', 0):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                convert_media(self.source, self.root / 'out.mp4', 'mp4', FFMPEG)
        self.assertEqual(list(self.root.glob('.convert-*')), [])
    def test_validation(self):
        with self.assertRaises(ValueError):
            convert_media(self.source, self.source, 'mp4', FFMPEG)
        with self.assertRaises(ValueError):
            convert_media(self.source, self.root / 'out.exe', 'exe', FFMPEG)

    def test_surround_audio_downmixes_to_mp3(self):
        source = self.root / 'surround.wav'
        subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-nostdin',
                        '-f', 'lavfi', '-i', 'anullsrc=channel_layout=5.1:sample_rate=48000',
                        '-t', '0.3', '-c:a', 'pcm_s16le', str(source)],
                       check=True, capture_output=True)
        target = self.root / 'stereo.mp3'
        convert_media(source, target, 'mp3', FFMPEG)
        result = subprocess.run([FFMPEG, '-hide_banner', '-i', str(target),
                                 '-f', 'null', '-'], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn('stereo', result.stderr.decode())
