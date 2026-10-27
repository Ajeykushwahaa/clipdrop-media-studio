"""ClipDrop: single-user local media downloader and converter."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from uuid import uuid4
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import re
import json
import os
import shutil

import imageio_ffmpeg
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field
from starlette.formparsers import MultiPartException
from yt_dlp import YoutubeDL
from media import OUTPUT_FORMATS, AUDIO_FORMATS, convert_media

ROOT = Path(__file__).resolve().parent
DOWNLOADS = Path(os.environ.get('CLIPDROP_DOWNLOADS', ROOT / 'downloads')).resolve()
DOWNLOADS.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = int(os.environ.get('CLIPDROP_MAX_UPLOAD_BYTES', 500 * 1024 * 1024))
MAX_BODY_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024
MAX_STORAGE_BYTES = int(os.environ.get('CLIPDROP_MAX_STORAGE_BYTES', 4 * 1024 * 1024 * 1024))
MAX_MEMORY_JOBS = 100
INPUT_EXTENSIONS = {'.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.mpeg', '.mpg', '.wmv', '.flv', '.ts', '.mts', '.m2ts', '.3gp', '.mp3', '.m4a', '.aac', '.wav', '.flac', '.ogg', '.opus', '.aiff', '.aif', '.wma'}
PLATFORMS = [
    {'name': 'YouTube', 'domains': ['youtube.com', 'youtu.be']},
    {'name': 'Facebook', 'domains': ['facebook.com', 'fb.watch']},
    {'name': 'Instagram', 'domains': ['instagram.com']},
    {'name': 'TikTok', 'domains': ['tiktok.com']},
    {'name': 'X / Twitter', 'domains': ['x.com', 'twitter.com']},
    {'name': 'Vimeo', 'domains': ['vimeo.com']},
    {'name': 'Dailymotion', 'domains': ['dailymotion.com', 'dai.ly']},
    {'name': 'Reddit', 'domains': ['reddit.com', 'redd.it']},
    {'name': 'Twitch', 'domains': ['twitch.tv']},
    {'name': 'SoundCloud', 'domains': ['soundcloud.com']},
]


def limit_label(size):
    return f'{size // (1024 * 1024)} MiB' if size < 1024 ** 3 else f'{size // (1024 ** 3)} GiB'


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def storage_bytes():
    return sum(path.stat().st_size for path in DOWNLOADS.rglob('*') if path.is_file())


def reconcile_downloads():
    """Remove interrupted temporary jobs and keep valid completed outputs."""
    for folder in DOWNLOADS.iterdir():
        if not folder.is_dir() or not re.fullmatch(r'[a-f0-9]{32}', folder.name):
            continue
        try:
            saved = json.loads((folder / 'job.json').read_text(encoding='utf-8'))
            filename = saved.get('filename')
            output = folder / filename if isinstance(filename, str) else None
            valid = (saved.get('status') == 'done' and output is not None
                     and Path(filename).name == filename and output.is_file()
                     and not output.is_symlink() and output.resolve().parent == folder.resolve())
        except (OSError, ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            shutil.rmtree(folder, ignore_errors=True)


@asynccontextmanager
async def lifespan(application):
    reconcile_downloads()
    yield
    # Cancel queued tasks. Running native/network work finishes in the background
    # without blocking the app shutdown path.
    executor.shutdown(wait=False, cancel_futures=True)


def ffmpeg_executable():
    return shutil.which('ffmpeg') or imageio_ffmpeg.get_ffmpeg_exe()


def javascript_runtimes():
    node = os.environ.get('CLIPDROP_NODE') or shutil.which('node')
    deno = shutil.which('deno')
    if deno:
        return {'deno': {'path': deno}}
    return {'node': {'path': node}} if node else {}


class UploadTooLarge(MultiPartException):
    pass


class UploadLimitMiddleware:
    """Bound multipart input even when Content-Length is absent."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') != '/api/conversions':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            return await JSONResponse({'detail': 'Invalid request length.'}, status_code=400)(scope, receive, send)
        if length > MAX_BODY_BYTES:
            return await JSONResponse({'detail': f'File is too large. Maximum upload is {limit_label(MAX_UPLOAD_BYTES)}.'}, status_code=413)(scope, receive, send)
        count = 0
        exceeded = False
        async def limited_receive():
            nonlocal count, exceeded
            message = await receive()
            if message['type'] == 'http.request':
                count += len(message.get('body', b''))
                if count > MAX_BODY_BYTES:
                    exceeded = True
                    raise UploadTooLarge('Maximum upload exceeded.')
            return message
        async def limited_send(message):
            if not exceeded:
                await send(message)
        try:
            await self.app(scope, limited_receive, limited_send)
        except UploadTooLarge:
            exceeded = True
        if exceeded:
            await JSONResponse({'detail': f'File is too large. Maximum upload is {limit_label(MAX_UPLOAD_BYTES)}.'}, status_code=413)(scope, receive, send)


app = FastAPI(title='ClipDrop — Media Studio', lifespan=lifespan)
app.add_middleware(UploadLimitMiddleware)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[
    host.strip() for host in os.environ.get(
        'CLIPDROP_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1],testserver').split(',') if host.strip()
])
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
executor = ThreadPoolExecutor(max_workers=2)
lock = Lock()
jobs = {}


def platform_for(host):
    for platform in PLATFORMS:
        if any(host == domain or host.endswith('.' + domain) for domain in platform['domains']):
            return platform['name']
    return None


def canonical_url(value: str) -> str:
    try:
        parsed = urlparse(value.strip())
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            raise ValueError('Enter a valid public video or audio link.')
        host = (parsed.hostname or '').lower().rstrip('.')
        platform = platform_for(host)
        if not platform:
            raise ValueError('Unsupported site. Choose a platform listed below the link field.')
        if platform == 'Facebook' and parsed.path.lower().rstrip('/') in ('/l.php', '/flx/warn'):
            raise ValueError('Paste a direct Facebook video or Reel link, not a redirect link.')
        if any(re.match(r'(?i)^https?://', item) for values in parse_qs(parsed.query).values() for item in values):
            raise ValueError('Paste a direct media link, not a redirect link.')
        if platform == 'YouTube':
            if host == 'youtu.be':
                video_id = parsed.path.strip('/')
            else:
                parts = parsed.path.strip('/').split('/')
                video_id = parse_qs(parsed.query).get('v', [''])[0] if parsed.path == '/watch' else (parts[1] if len(parts) == 2 and parts[0] in ('shorts', 'embed', 'live') else '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
                raise ValueError('This YouTube link does not contain a valid video ID.')
            return f'https://www.youtube.com/watch?v={video_id}'
        if not parsed.path.strip('/'):
            if platform != 'Facebook' or not parse_qs(parsed.query).get('v'):
                raise ValueError('Paste a direct video, Reel, clip, or track link, rather than the site homepage.')
        # Preserve content identifiers; discard common analytics and playlist parameters.
        query = [(key, item) for key, values in parse_qs(parsed.query).items()
                 if not key.startswith('utm_') and key not in ('fbclid', 'igsh', 'igshid', 'list', 'start_radio') for item in values]
        return urlunparse(('https', host, parsed.path, '', urlencode(query), ''))
    except ValueError as error:
        if str(error).startswith(('Port ', 'Invalid ', 'Unmatched ')):
            raise ValueError('Enter a valid public video or audio link.') from error
        raise


class DownloadRequest(BaseModel):
    url: str = Field(max_length=4096)
    quality: str = '720'
    output_format: str = 'original'


def update(job_id, **values):
    with lock:
        jobs[job_id].update(values)


def prune_jobs():
    terminal = [(job_id, job) for job_id, job in jobs.items()
                if job.get('status') in ('done', 'error')]
    terminal.sort(key=lambda item: item[1].get('updated_at', ''))
    for job_id, _ in terminal[:-MAX_MEMORY_JOBS]:
        jobs.pop(job_id, None)


def reserve_job(**values):
    with lock:
        prune_jobs()
        if sum(job['status'] not in ('done', 'error') for job in jobs.values()) >= 4:
            raise HTTPException(429, 'The queue is full. Please wait for a task to finish.')
        if storage_bytes() >= MAX_STORAGE_BYTES:
            raise HTTPException(507, f'ClipDrop storage has reached {limit_label(MAX_STORAGE_BYTES)}. Remove old files from the downloads folder before starting another task.')
        job_id = uuid4().hex
        jobs[job_id] = dict(status='queued', progress=0, message='Waiting to start…',
                            created_at=now_iso(), updated_at=now_iso(), **values)
    return job_id


def ensure_ffmpeg():
    try:
        return ffmpeg_executable()
    except (RuntimeError, OSError) as error:
        raise HTTPException(503, 'FFmpeg is unavailable. Reinstall requirements.txt and restart the app.') from error


def finish_job(job_id, path, **values):
    if not path.is_file() or not path.stat().st_size:
        raise RuntimeError('No complete media file was produced. Try another format.')
    completed = dict(jobs[job_id], status='done', progress=100, message='Your file is ready.',
                     filename=path.name, filesize=path.stat().st_size,
                     format=path.suffix.lstrip('.'), updated_at=now_iso(), **values)
    try:
        metadata = path.parent / 'job.json.tmp'
        metadata.write_text(json.dumps(completed), encoding='utf-8')
        metadata.replace(path.parent / 'job.json')
    except OSError as error:
        raise RuntimeError('The file was created, but its job record could not be saved. Check folder permissions.') from error
    with lock:
        jobs[job_id] = completed


def clean_error(error):
    message = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(error))
    message = re.sub(r'^ERROR:\s*', '', message.strip())
    # yt-dlp prepends extractor diagnostics; retain the concrete upstream reason.
    if any(word in message.lower() for word in ('login required', 'log in', 'sign in', 'cookies', 'private video')):
        return 'This source requires a login or is restricted. Try a public, directly accessible video. Account cookies are not imported.'
    message = re.sub(r'https?://\S+', '[media link]', message)
    message = message.replace(str(ROOT), '[app folder]')
    message = re.sub(r'(?<![\w:])/(?:[^\s/:]+/)*[^\s:]+', '[local path]', message)
    if len(message) > 240:
        return 'The media tool could not process this source. The link may be unavailable, restricted, or temporarily unsupported.'
    return message or 'The task failed. Please try again.'


def submit_job(job_id, function, *args):
    try:
        future = executor.submit(function, *args)
    except RuntimeError as error:
        update(job_id, status='error', updated_at=now_iso(), message='The task worker is unavailable. Restart the server.')
        raise HTTPException(503, 'The task worker is unavailable. Restart the server.') from error
    def completed(done):
        try:
            failure = done.exception()
        except Exception as error:
            failure = error
        with lock:
            job = jobs.get(job_id)
            if failure and job and job.get('status') not in ('done', 'error'):
                job.update(status='error', updated_at=now_iso(), message='The background task stopped unexpectedly. Please retry.')
    # Real executors return a Future. Keeping direct/synchronous executors valid
    # also makes the worker boundary easy to exercise without threads.
    if hasattr(future, 'add_done_callback'):
        future.add_done_callback(completed)


def download(job_id, url, quality, output_format='original'):
    folder = DOWNLOADS / job_id
    def progress(data):
        if data['status'] == 'downloading':
            total = data.get('total_bytes') or data.get('total_bytes_estimate')
            percent = min(85, round(data.get('downloaded_bytes', 0) / total * 85)) if total else 0
            title = (data.get('info_dict') or {}).get('title')
            values = {'title': title} if title else {}
            update(job_id, status='downloading', progress=percent, message='Downloading media…', **values)
        elif data['status'] == 'finished':
            update(job_id, status='processing', message='Preparing your file…')
    if quality == 'mp3':  # Keep existing bookmarked/API clients compatible.
        quality, output_format = '720', 'mp3'
    if output_format in AUDIO_FORMATS:
        format_selector = 'bestaudio/best'
    else:
        format_selector = f'bv*[height<={quality}]+ba/b[height<={quality}]'
        if output_format == 'original':
            format_selector += '/bestaudio'
    options = {
        'outtmpl': str(folder / 'source.%(ext)s'), 'noplaylist': True, 'playlist_items': '1',
        'quiet': True, 'no_warnings': True, 'noprogress': True, 'color': 'no_color',
        'socket_timeout': 30, 'retries': 3, 'restrictfilenames': True,
        'progress_hooks': [progress], 'max_filesize': 1024 * 1024 * 1024,
        'format': format_selector,
        'merge_output_format': 'mkv',
    }
    try:
        folder.mkdir()
        ffmpeg = ffmpeg_executable()
        options.update(ffmpeg_location=ffmpeg, js_runtimes=javascript_runtimes())
        update(job_id, status='downloading', message='Connecting to the source…')
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
        if not isinstance(info, dict) or info.get('_type') in ('playlist', 'multi_video') or info.get('entries') is not None:
            raise RuntimeError('This link contains a collection. Paste a link to one video or track.')
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix not in ('.part', '.ytdl', '.temp', '.json')]
        if len(files) != 1:
            raise RuntimeError('No single complete media file was produced. Try a direct video link.')
        source = files[0]
        title = info.get('title') or 'Media'
        slug = re.sub(r'[^\w-]+', '_', title, flags=re.ASCII).strip('_')[:100] or 'media'
        if output_format == 'original':
            target = folder / f'{slug}{source.suffix}'
            source.rename(target)
        else:
            target = folder / f'{slug}.{output_format}'
            update(job_id, status='processing', progress=90, title=title, message=f'Converting to {output_format.upper()}…')
            convert_media(source, target, output_format, ffmpeg)
            source.unlink(missing_ok=True)
        finish_job(job_id, target, title=title, quality=quality, duration=info.get('duration'),
                   kind='download', source=platform_for(urlparse(url).hostname or ''))
    except Exception as error:
        update(job_id, status='error', updated_at=now_iso(), message=clean_error(error))
        shutil.rmtree(folder, ignore_errors=True)


def convert_uploaded(job_id, source, output_format, title):
    folder = DOWNLOADS / job_id
    try:
        update(job_id, status='processing', progress=0, message=f'Converting to {output_format.upper()}…')
        target = folder / f'converted.{output_format}'
        convert_media(source, target, output_format, ffmpeg_executable())
        source.unlink(missing_ok=True)
        finish_job(job_id, target, title=title, kind='conversion', source='Uploaded file', quality=None)
    except Exception as error:
        update(job_id, status='error', updated_at=now_iso(), message=clean_error(error))
        shutil.rmtree(folder, ignore_errors=True)


@app.get('/api/health')
def health():
    try:
        ffmpeg_ready = bool(ffmpeg_executable())
    except (RuntimeError, OSError):
        ffmpeg_ready = False
    runtimes = javascript_runtimes()
    return {'status': 'ok' if ffmpeg_ready and runtimes else 'degraded',
            'ffmpeg': ffmpeg_ready, 'javascript': bool(runtimes),
            'javascript_runtime': next(iter(runtimes), None)}


@app.get('/api/capabilities')
def capabilities():
    return {'platforms': PLATFORMS, 'formats': [
        {'id': key, 'label': value['label'], 'kind': 'audio' if key in AUDIO_FORMATS else 'video'}
        for key, value in OUTPUT_FORMATS.items()], 'max_upload_bytes': MAX_UPLOAD_BYTES}


@app.get('/')
def index():
    return FileResponse(ROOT / 'static/index.html', headers={'Cache-Control': 'no-cache'})


@app.post('/api/downloads', status_code=202)
def start(body: DownloadRequest):
    if body.quality not in ('360', '720', '1080', 'mp3'):
        raise HTTPException(400, 'Choose a supported quality.')
    if body.output_format not in ('original', *OUTPUT_FORMATS):
        raise HTTPException(400, 'Choose a supported output format.')
    try:
        url = canonical_url(body.url)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    ensure_ffmpeg()
    output_format = 'mp3' if body.quality == 'mp3' else body.output_format
    job_id = reserve_job(quality=body.quality, kind='download', source=platform_for(urlparse(url).hostname or ''), output_format=output_format)
    submit_job(job_id, download, job_id, url, body.quality, output_format)
    return {'id': job_id}


@app.post('/api/conversions', status_code=202)
async def upload_conversion(file: UploadFile = File(...), output_format: str = Form('mp3')):
    job_id = None
    folder = None
    try:
        if output_format not in OUTPUT_FORMATS:
            raise HTTPException(400, 'Choose a supported output format.')
        # Name is never used as a filesystem path.
        filename = (file.filename or 'media').replace('\\', '/').rsplit('/', 1)[-1]
        extension = Path(filename).suffix.lower()
        if extension not in INPUT_EXTENSIONS:
            raise HTTPException(400, 'Choose an audio or video file, such as MP4, MOV, MKV, WebM, MP3, WAV, or FLAC.')
        ensure_ffmpeg()
        job_id = reserve_job(kind='conversion', title=filename[:200], source='Uploaded file', output_format=output_format, quality=None)
        folder = DOWNLOADS / job_id
        folder.mkdir()
        source = folder / f'input{extension}'
        total = 0
        with source.open('wb') as destination:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f'File is too large. Maximum upload is {limit_label(MAX_UPLOAD_BYTES)}.')
                destination.write(chunk)
        if not total:
            raise HTTPException(400, 'The uploaded file is empty.')
        submit_job(job_id, convert_uploaded, job_id, source, output_format, filename[:200])
        return {'id': job_id}
    except Exception as error:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        if job_id:
            update(job_id, status='error', updated_at=now_iso(), message=clean_error(error))
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(500, 'The file could not be queued. Please try again.') from error
    finally:
        await file.close()


@app.get('/api/downloads/{job_id}')
def status(job_id: str):
    with lock:
        if job_id not in jobs and re.fullmatch(r'[a-f0-9]{32}', job_id):
            try:
                saved = json.loads((DOWNLOADS / job_id / 'job.json').read_text(encoding='utf-8'))
                filename = saved.get('filename')
                if (saved.get('status') == 'done' and isinstance(filename, str)
                        and Path(filename).name == filename
                        and (DOWNLOADS / job_id / filename).is_file()
                        and not (DOWNLOADS / job_id / filename).is_symlink()
                        and (DOWNLOADS / job_id / filename).resolve().parent == (DOWNLOADS / job_id).resolve()):
                    jobs[job_id] = saved
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        if job_id not in jobs:
            raise HTTPException(404, 'Task not found. Active tasks reset when the app restarts.')
        return dict(jobs[job_id])


@app.get('/api/downloads/{job_id}/file')
def file(job_id: str):
    job = status(job_id)
    if job['status'] != 'done':
        raise HTTPException(409, 'The file is not ready yet.')
    path = DOWNLOADS / job_id / job['filename']
    folder = (DOWNLOADS / job_id).resolve()
    if (not path.is_file() or path.is_symlink()
            or path.resolve().parent != folder):
        raise HTTPException(404, 'File no longer exists.')
    return FileResponse(path, filename=job['filename'], media_type='application/octet-stream')
