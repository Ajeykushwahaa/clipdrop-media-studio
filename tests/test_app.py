import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import app, canonical_url, jobs, download, DOWNLOADS


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        jobs.clear()

    def test_normalizes_video_urls(self):
        expected = 'https://www.youtube.com/watch?v=abcdefghijk'
        for url in ('https://youtu.be/abcdefghijk?t=20', 'https://www.youtube.com/shorts/abcdefghijk', 'https://youtube.com/watch?v=abcdefghijk&list=PL123'):
            self.assertEqual(canonical_url(url), expected)

    def test_rejects_untrusted_or_invalid_urls(self):
        for url in ('https://youtube.com.evil.test/watch?v=abcdefghijk', 'file:///etc/passwd', 'https://youtube.com/playlist?list=123', 'https://youtu.be/short', 'https://user@youtube.com/watch?v=abcdefghijk', 'https://youtube.com:99/watch?v=abcdefghijk', 'https://facebook.com/l.php?u=http%3A%2F%2F127.0.0.1%2Fprivate'):
            with self.assertRaises(ValueError):
                canonical_url(url)

    @patch('app.storage_bytes', return_value=4 * 1024 * 1024 * 1024)
    @patch('app.ffmpeg_executable', return_value='/bin/ffmpeg')
    def test_storage_quota_rejects_new_job(self, ffmpeg, used):
        response = self.client.post('/api/downloads', json={'url': 'https://youtu.be/abcdefghijk'})
        self.assertEqual(response.status_code, 507)
        self.assertIn('4 GiB', response.json()['detail'])

    def test_home_and_missing_job(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/api/downloads/unknown').status_code, 404)

    def test_invalid_input(self):
        self.assertEqual(self.client.post('/api/downloads', json={'url': 'https://example.com'}).status_code, 400)
        self.assertEqual(self.client.post('/api/downloads', json={'url': 'https://youtu.be/abcdefghijk', 'quality': 'bad'}).status_code, 400)

    @patch('app.shutil.which', return_value='/bin/ffmpeg')
    @patch('app.executor.submit')
    def test_queue_and_not_ready_file(self, submit, which):
        for _ in range(4):
            response = self.client.post('/api/downloads', json={'url': 'https://youtu.be/abcdefghijk'})
            self.assertEqual(response.status_code, 202)
        job_id = response.json()['id']
        self.assertEqual(self.client.get(f'/api/downloads/{job_id}').json()['status'], 'queued')
        self.assertEqual(self.client.get(f'/api/downloads/{job_id}/file').status_code, 409)
        self.assertEqual(self.client.post('/api/downloads', json={'url': 'https://youtu.be/abcdefghijk'}).status_code, 429)
        self.assertEqual(submit.call_count, 4)

    @patch('app.ffmpeg_executable', return_value='/test/ffmpeg')
    @patch('app.YoutubeDL')
    def test_successful_download_and_file(self, ydl, ffmpeg):
        job_id = '1234567890abcdef1234567890abcdef'
        jobs[job_id] = {'status': 'queued', 'progress': 0}
        def extract(*args, **kwargs):
            (DOWNLOADS / job_id / 'example.webm').write_bytes(b'test-media')
            return {'title': 'Example'}
        ydl.return_value.__enter__.return_value.extract_info.side_effect = extract
        try:
            download(job_id, 'https://www.youtube.com/watch?v=abcdefghijk', '720')
            self.assertEqual(jobs[job_id]['status'], 'done')
            self.assertEqual(jobs[job_id]['title'], 'Example')
            self.assertEqual(jobs[job_id]['quality'], '720')
            self.assertEqual(jobs[job_id]['filesize'], len(b'test-media'))
            self.assertEqual(jobs[job_id]['format'], 'webm')
            self.assertTrue(ydl.call_args.args[0]['noprogress'])
            self.assertEqual(self.client.get(f'/api/downloads/{job_id}/file').content, b'test-media')
            jobs.clear()
            self.assertEqual(self.client.get(f'/api/downloads/{job_id}').json()['title'], 'Example')
            self.assertEqual(self.client.get(f'/api/downloads/{job_id}/file').content, b'test-media')
        finally:
            import shutil
            shutil.rmtree(DOWNLOADS / job_id, ignore_errors=True)

    @patch('app.ffmpeg_executable', return_value='/test/ffmpeg')
    @patch('app.YoutubeDL')
    def test_error_cleans_partial_files(self, ydl, ffmpeg):
        jobs['failure'] = {'status': 'queued', 'progress': 0}
        ydl.return_value.__enter__.return_value.extract_info.side_effect = RuntimeError('\x1b[31mERROR: Unavailable\x1b[0m')
        download('failure', 'https://www.youtube.com/watch?v=abcdefghijk', 'mp3')
        self.assertEqual(jobs['failure']['status'], 'error')
        self.assertFalse((DOWNLOADS / 'failure').exists())
        self.assertEqual(jobs['failure']['message'], 'Unavailable')

    @patch('app.javascript_runtimes', return_value={'node': {'path': '/private/node'}})
    @patch('app.ffmpeg_executable', return_value='/private/ffmpeg')
    def test_health_ready_without_paths(self, ffmpeg, runtimes):
        result = self.client.get('/api/health')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {'status': 'ok', 'ffmpeg': True,
                                        'javascript': True, 'javascript_runtime': 'node'})
        self.assertNotIn('/private', result.text)

    @patch('app.javascript_runtimes', return_value={})
    @patch('app.ffmpeg_executable', side_effect=RuntimeError('missing'))
    def test_health_degraded(self, ffmpeg, runtimes):
        self.assertEqual(self.client.get('/api/health').json(), {
            'status': 'degraded', 'ffmpeg': False, 'javascript': False,
            'javascript_runtime': None})

    @patch('app.ffmpeg_executable', side_effect=RuntimeError('missing'))
    def test_worker_setup_failure_is_reported(self, ffmpeg):
        jobs['setupfailure'] = {'status': 'queued', 'progress': 0}
        download('setupfailure', 'https://www.youtube.com/watch?v=abcdefghijk', '720')
        self.assertEqual(jobs['setupfailure']['status'], 'error')
        self.assertFalse((DOWNLOADS / 'setupfailure').exists())


if __name__ == '__main__':
    unittest.main()
