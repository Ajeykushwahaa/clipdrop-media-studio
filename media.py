"""Local-only, bounded FFmpeg conversions for common media formats."""
from pathlib import Path
import os
import subprocess
import tempfile
import threading
from typing import Callable

AUDIO_FORMATS = {'mp3', 'm4a', 'wav', 'flac', 'ogg'}
OUTPUT_FORMATS = {
    key: {'label': key.upper(), 'extension': key, 'media_type': kind, 'mime_type': mime}
    for key, kind, mime in (
        ('mp4', 'video', 'video/mp4'), ('mkv', 'video', 'video/x-matroska'),
        ('webm', 'video', 'video/webm'), ('mov', 'video', 'video/quicktime'),
        ('mp3', 'audio', 'audio/mpeg'), ('m4a', 'audio', 'audio/mp4'),
        ('wav', 'audio', 'audio/wav'), ('flac', 'audio', 'audio/flac'),
        ('ogg', 'audio', 'audio/ogg'),
    )
}
CONVERSION_TIMEOUT = 30 * 60
MAX_OUTPUT_BYTES = 1024 * 1024 * 1024


def convert_media(source: Path, target: Path, output_format: str, ffmpeg: str,
                  on_progress: Callable[[int | None], None] | None = None) -> None:
    """Convert a local file atomically; raise an actionable error on failure.

    Progress is indeterminate until successful completion. Existing target files
    remain untouched on failure. Remote input protocols are disabled.
    """
    source, target = Path(source).resolve(), Path(target).resolve()
    if output_format not in OUTPUT_FORMATS:
        raise ValueError('Unsupported output format.')
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError('The input file is missing or empty.')
    if source == target:
        raise ValueError('Input and output must be different files.')
    if not target.parent.is_dir():
        raise ValueError('The output directory does not exist.')

    def report(value):
        if on_progress:
            try:
                on_progress(value)
            except Exception:
                pass  # UI notification failure must not leave FFmpeg running.

    audio = output_format in AUDIO_FORMATS
    args = [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-protocol_whitelist', 'file,pipe', '-i', str(source),
            '-map_metadata', '-1', '-map_chapters', '-1']
    if audio:
        codecs = {'mp3': 'libmp3lame', 'm4a': 'aac', 'wav': 'pcm_s16le',
                  'flac': 'flac', 'ogg': 'libvorbis'}
        args += ['-map', '0:a:0', '-vn', '-c:a', codecs[output_format]]
        if output_format == 'mp3':
            args += ['-ac', '2']  # MP3 encoders support at most stereo.
        if output_format in {'mp3', 'm4a'}:
            args += ['-b:a', '192k']
    else:
        args += ['-map', '0:v:0', '-map', '0:a:0?', '-sn', '-dn']
        if output_format == 'webm':
            args += ['-c:v', 'libvpx-vp9', '-crf', '32', '-b:v', '0',
                     '-deadline', 'realtime', '-cpu-used', '6', '-c:a', 'libopus']
        else:
            args += ['-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', '-c:v', 'libx264',
                     '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p', '-c:a', 'aac']
            if output_format in {'mp4', 'mov'}:
                args += ['-movflags', '+faststart']
    muxer = {'mkv': 'matroska', 'm4a': 'ipod'}.get(output_format, output_format)
    fd, temporary = tempfile.mkstemp(prefix='.convert-', suffix='.' + output_format,
                                      dir=target.parent)
    os.close(fd)
    process = None
    reader = None
    try:
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(args + ['-fs', str(MAX_OUTPUT_BYTES),
                                                '-progress', 'pipe:1', '-nostats',
                                                '-f', muxer, temporary],
                                       stdout=subprocess.PIPE, stderr=errors, text=True)
            def read_progress():
                for line in process.stdout:
                    if line.startswith('progress='):
                        report(None)
            reader = threading.Thread(target=read_progress, daemon=True)
            reader.start()
            report(None)
            try:
                code = process.wait(timeout=CONVERSION_TIMEOUT)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                raise RuntimeError('Conversion timed out after 30 minutes. Try a shorter file.') from exc
            reader.join(timeout=5)
            errors.seek(0, os.SEEK_END)
            size = errors.tell()
            errors.seek(max(0, size - 6000))
            detail = errors.read().decode('utf-8', errors='replace').strip()
            if code:
                if 'matches no streams' in detail or 'does not contain any stream' in detail:
                    required = 'audio' if audio else 'video'
                    raise RuntimeError(f'The input file has no usable {required} track.')
                raise RuntimeError('FFmpeg could not convert this file. ' + (detail[-2000:] or 'The file may be damaged or unsupported.'))
            if not Path(temporary).is_file() or Path(temporary).stat().st_size == 0:
                raise RuntimeError('Conversion produced an empty file.')
            if Path(temporary).stat().st_size >= MAX_OUTPUT_BYTES:
                raise RuntimeError('Converted file reached the 1 GiB output limit. Try a smaller file or compressed format.')
            os.replace(temporary, target)
            report(100)
    except FileNotFoundError as exc:
        raise RuntimeError('FFmpeg is unavailable. Install FFmpeg and restart the app.') from exc
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if reader:
            reader.join(timeout=5)
        if process is not None and process.stdout:
            process.stdout.close()
        Path(temporary).unlink(missing_ok=True)
