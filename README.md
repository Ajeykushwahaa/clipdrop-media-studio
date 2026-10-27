# ClipDrop — All-in-one Media Studio

A local web app for downloading public video/audio links and converting your own media files. FastAPI + yt-dlp + FFmpeg, with a responsive dark dashboard, drag/drop uploads, upload progress, saved jobs, and recent files.

## Features

- Download links from YouTube/Shorts, Facebook videos/Reels, Instagram posts/Reels, TikTok, X/Twitter, Vimeo, Dailymotion, Reddit, Twitch clips/videos, and SoundCloud tracks.
- Download as MP4, MKV, WebM, MOV, MP3, M4A, WAV, FLAC, OGG, or keep the original media format.
- Upload local video/audio and convert it to any of the nine listed outputs. Extract MP3 from video in the same interface.
- Select up to 360p, 720p, or 1080p when downloading video. Conversion does not increase source quality.
- Saved completed jobs reopen after restart. Recent-file list lives in your browser.

These are supported common audio/video formats, not a universal converter for documents, archives, or every possible file extension. Audio-only inputs cannot produce video; inputs without audio cannot produce audio outputs. Input codecs must be supported by the bundled FFmpeg build.

## Run locally

Install Python 3.10+ and Node.js 20+ or Deno. FFmpeg comes with the `imageio-ffmpeg` wheel on supported platforms; a system FFmpeg installation also works. On macOS with Homebrew:

```sh
brew install python node
```

From this repository:

```sh
python3 -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 and leave the server running. If Node is outside PATH, set `CLIPDROP_NODE` to its absolute executable path before launch. API documentation: http://127.0.0.1:8000/docs.

## Deploy on Render

The included `Dockerfile` and `render.yaml` deploy the complete FastAPI, yt-dlp, Node.js, and FFmpeg app as a public Render web service. Push this repository to GitHub, then create a Render Blueprint from that repository. Render supplies the public HTTPS URL.

The default Blueprint uses Render's free plan. Its filesystem is temporary, so generated files disappear when the service sleeps, restarts, or redeploys. The Blueprint limits uploads to 100 MiB and generated storage to 500 MiB. Use a paid persistent disk and set `CLIPDROP_DOWNLOADS=/data` if files must survive restarts.

## Supported inputs and outputs

| Output | Behavior |
| --- | --- |
| MP4 / MOV | H.264 video, AAC audio, browser-friendly fast start |
| MKV | H.264 video, AAC audio in Matroska |
| WebM | VP9 video, Opus audio |
| MP3 | 192 kbps stereo audio |
| M4A | 192 kbps AAC audio |
| WAV | PCM audio |
| FLAC | Lossless audio encoding |
| OGG | Vorbis audio |
| Original | Download only; keep extracted container and codecs |

Lossless encoding cannot restore quality lost in the source. Common upload extensions include MP4, MKV, WebM, MOV, AVI, M4V, MPEG/MPG, WMV, FLV, TS/MTS/M2TS, 3GP, MP3, M4A, AAC, WAV, FLAC, OGG, Opus, AIFF, and WMA. File extensions are a first check; FFmpeg validates the actual content.

## Platform availability

Platform support comes from yt-dlp's extractors. The app accepts the listed domains; a specific link may still fail if the platform changes, restricts the content, requires login, rate-limits requests, or blocks the current region. Facebook and Instagram may require authentication even for some publicly visible posts. No account cookie import, login automation, or DRM bypass is included. Use individual media links; profiles, playlists, live streams, and collections are outside the supported flow. Collections are limited to one entry and then rejected.

Only download or convert content you own or have permission to save. A public deployment can consume substantial bandwidth and compute; monitor usage and add authentication before sharing it widely.

## Limits and storage

- Local uploads: maximum 500 MiB, with a streaming request-body limit and actual file-size check. Upload progress is shown; conversion progress is indeterminate.
- Two processing workers; at most four queued/active tasks.
- Conversion timeout: 30 minutes. Longer encodes fail with a clear message.
- Downloads and conversions have a 1 GiB output limit. New jobs are rejected when the generated `downloads/` folder reaches 4 GiB.
- Files and completed metadata remain in `downloads/<job-id>/`. Remove them manually to reclaim space. Uploaded source files are deleted after conversion or on failure.
- Interrupted job folders are removed at startup. Completed jobs persist in `job.json`; use a single server worker process.
- History stored in this browser may refer to manually deleted files. **Clear list** only removes the browser's list; it does not delete saved files.

## API

```text
GET  /api/health                    Tool readiness
GET  /api/capabilities              Accepted platforms and output formats
POST /api/downloads                JSON {url, quality, output_format}
POST /api/conversions              Multipart {file, output_format}
GET  /api/downloads/{id}            Shared task status
GET  /api/downloads/{id}/file       Save completed output
```

Example download body:

```json
{"url":"https://www.facebook.com/reel/123456789/","quality":"720","output_format":"mp4"}
```

This example demonstrates the URL structure; it is not a tested media URL. `quality: "mp3"` remains accepted for older clients. Download API defaults to original format; the website defaults to MP4.

## Verification

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Tests include real FFmpeg encoding and decoding for all nine outputs, surround-to-MP3 downmix, conversion failure/timeout handling, upload validation and limits (including chunked bodies), job persistence, platform routing, and download APIs with mocked extraction. The tests do not contact social platforms. A live YouTube-to-MP3 download and a browser MP4-upload-to-MP3 conversion were tested during development; Facebook/Reels and the other platforms have routing/extractor integration but have not been verified against a live media link.

## Research and upstream repositories

The project builds on official upstream libraries; no third-party downloader site is scraped or embedded.

- [yt-dlp source repository](https://github.com/yt-dlp/yt-dlp)
- [yt-dlp supported extractors](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)
- [yt-dlp Python embedding](https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp)
- [FFmpeg conversion documentation](https://ffmpeg.org/ffmpeg.html)
- [FFmpeg source repository](https://github.com/FFmpeg/FFmpeg)
- [Bundled FFmpeg wrapper source](https://github.com/imageio/imageio-ffmpeg)

Update extraction support with `python -m pip install -U 'yt-dlp[default]'`. Upstream dependencies retain their own licenses.

## Repository layout

- `app.py` — platform validation, API, bounded uploads, workers, job persistence
- `media.py` — explicit FFmpeg encoders and conversion timeout/cleanup
- `static/` — responsive media studio, upload/download/converter UI
- `tests/` — real media fixtures and offline API tests
- `requirements.txt` / `requirements-dev.txt` — runtime/test dependencies
- `Dockerfile` / `render.yaml` — public Docker deployment

The local folder is initialized as a Git repository. ZIP distributions omit Git metadata and generated downloads.
