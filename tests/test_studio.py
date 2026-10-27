"""API integration checks with local fixtures; no external services."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import shutil
import subprocess
import unittest
from fastapi.testclient import TestClient
import app as studio

class StudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = TemporaryDirectory()
        cls.fixture = Path(cls.fixture_dir.name) / 'sample.mp4'
        subprocess.run([studio.ffmpeg_executable(), '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'color=c=green:s=64x64:r=10', '-f', 'lavfi', '-i', 'sine=frequency=440',
                        '-t', '0.3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                        str(cls.fixture)], check=True, capture_output=True)
    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.patch = patch.object(studio, 'DOWNLOADS', Path(self.folder.name))
        self.patch.start()
        studio.jobs.clear()
        self.client = TestClient(studio.app)
    def tearDown(self):
        self.patch.stop(); self.folder.cleanup(); studio.jobs.clear()
    def test_capabilities(self):
        data = self.client.get('/api/capabilities').json()
        self.assertIn('Facebook', [p['name'] for p in data['platforms']])
        self.assertEqual(len(data['formats']), 9)
        self.assertEqual(data['max_upload_bytes'], 524288000)
    def test_supported_platform_links(self):
        for url in ('https://www.facebook.com/reel/123456789/', 'https://fb.watch/example/',
                    'https://www.instagram.com/reel/ABC123/', 'https://www.tiktok.com/@example/video/123456789',
                    'https://x.com/example/status/123456789', 'https://vimeo.com/123456789',
                    'https://www.dailymotion.com/video/x123456', 'https://www.reddit.com/r/test/comments/123/test/',
                    'https://clips.twitch.tv/ExampleClip', 'https://soundcloud.com/example/track'):
            with self.subTest(url=url): self.assertTrue(studio.canonical_url(url).startswith('https://'))
        for url in ('http://127.0.0.1/video.mp4','https://facebook.com.evil.test/reel/123',
                    'https://facebook.com/', 'https://youtube.com:invalid/watch?v=abcdefghijk'):
            with self.subTest(url=url), self.assertRaises(ValueError): studio.canonical_url(url)
    def test_reconcile_removes_interrupted_job_and_keeps_completed_job(self):
        partial = studio.DOWNLOADS / ('a' * 32); partial.mkdir(); (partial / 'partial.tmp').write_bytes(b'x')
        complete = studio.DOWNLOADS / ('b' * 32); complete.mkdir(); (complete / 'file.mp3').write_bytes(b'audio')
        (complete / 'job.json').write_text(json.dumps({'status':'done','filename':'file.mp3'}), encoding='utf-8')
        studio.reconcile_downloads()
        self.assertFalse(partial.exists()); self.assertTrue(complete.exists())
    def test_restored_symlink_cannot_escape_download_folder(self):
        job_id = 'c' * 32; folder = studio.DOWNLOADS / job_id; folder.mkdir()
        outside = studio.DOWNLOADS / 'outside.mp3'; outside.write_bytes(b'secret')
        link = folder / 'file.mp3'
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks are unavailable on this platform')
        (folder / 'job.json').write_text(json.dumps({'status':'done','filename':'file.mp3'}), encoding='utf-8')
        self.assertEqual(self.client.get('/api/downloads/'+job_id).status_code, 404)
        self.assertEqual(self.client.get('/api/downloads/'+job_id+'/file').status_code, 404)
    @patch('app.executor.submit')
    def test_facebook_is_queued_with_format(self, submit):
        response = self.client.post('/api/downloads', json={'url': 'https://www.facebook.com/reel/123456789/', 'quality': '720', 'output_format': 'mp4'})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(submit.call_args.args[-1], 'mp4')
        self.assertEqual(self.client.get('/api/downloads/'+response.json()['id']).json()['source'], 'Facebook')
    @patch('app.executor.submit')
    def test_upload_converts_and_survives_restart(self, submit):
        submit.side_effect = lambda function, *args: function(*args)
        response = self.client.post('/api/conversions', files={'file': ('my-video.mp4', self.fixture.read_bytes(), 'video/mp4')}, data={'output_format': 'mp3'})
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()['id']; job = self.client.get('/api/downloads/'+job_id).json()
        self.assertEqual(job['status'], 'done', job)
        self.assertEqual(job['kind'], 'conversion'); self.assertEqual(job['format'], 'mp3'); self.assertIsNone(job['quality'])
        self.assertFalse((studio.DOWNLOADS / job_id / 'input.mp4').exists())
        media = self.client.get('/api/downloads/'+job_id+'/file')
        self.assertEqual(media.status_code, 200); self.assertGreater(len(media.content), 100)
        studio.jobs.clear()
        self.assertEqual(self.client.get('/api/downloads/'+job_id).json()['status'], 'done')
    @patch('app.executor.submit')
    def test_bad_media_surfaces_failure(self, submit):
        submit.side_effect = lambda function, *args: function(*args)
        response = self.client.post('/api/conversions', files={'file': ('bad.mp4', b'not media')}, data={'output_format': 'mp3'})
        job_id = response.json()['id']; job = self.client.get('/api/downloads/'+job_id).json()
        self.assertEqual(job['status'], 'error'); self.assertFalse((studio.DOWNLOADS/job_id).exists())
        self.assertEqual(self.client.get('/api/downloads/'+job_id+'/file').status_code, 409)
    def test_upload_validation(self):
        for filename, content, fmt in (('file.txt',b'hello','mp3'),('empty.mp4',b'','mp3'),('video.mp4',b'abc','exe')):
            response = self.client.post('/api/conversions', files={'file': (filename,content)}, data={'output_format': fmt})
            self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse(any(studio.DOWNLOADS.iterdir()))
    @patch('app.MAX_UPLOAD_BYTES', 10)
    def test_upload_file_size_limit(self):
        response = self.client.post('/api/conversions', files={'file': ('file.mp4',b'x'*11)}, data={'output_format':'mp3'})
        self.assertEqual(response.status_code,413,response.text); self.assertFalse(any(studio.DOWNLOADS.iterdir()))
    @patch('app.MAX_BODY_BYTES', 20)
    def test_body_limit_without_content_length(self):
        def body():
            yield b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="x.mp4"\r\n\r\n'
            yield b'x'*30
            yield b'\r\n--boundary--\r\n'
        response = self.client.post('/api/conversions', content=body(), headers={'Content-Type': 'multipart/form-data; boundary=boundary'})
        self.assertEqual(response.status_code,413,response.text)
    @patch('app.YoutubeDL')
    def test_download_and_mp4_conversion(self, ydl):
        job_id = studio.reserve_job(kind='download')
        def extract(*args, **kwargs):
            shutil.copyfile(self.fixture, studio.DOWNLOADS / job_id / 'source.mp4')
            return {'title':'Local fixture', 'duration':0.3}
        ydl.return_value.__enter__.return_value.extract_info.side_effect=extract
        studio.download(job_id,'https://www.facebook.com/reel/123/', '720','mp4')
        job = studio.status(job_id)
        self.assertEqual(job['status'],'done',job); self.assertEqual(job['source'],'Facebook'); self.assertEqual(job['format'],'mp4')
        self.assertEqual(len(list((studio.DOWNLOADS/job_id).glob('*.mp4'))),1)
        self.assertEqual(ydl.call_args.args[0]['playlist_items'],'1')

if __name__ == '__main__': unittest.main()
