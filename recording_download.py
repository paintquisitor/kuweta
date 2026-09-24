"""Isolated Tapo download worker. Credentials never leave the environment file."""
import asyncio
import contextlib
import importlib
import io
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path


class RecordingUnavailable(Exception):
    """The exact archived interval no longer exists on the camera."""


async def require_recording(tapo, date, start, end):
    index, seen, found = 0, set(), False
    while True:
        page = await asyncio.to_thread(tapo.getRecordings, date,
                                       start_index=index, end_index=index + 99)
        for item in page:
            record = next(iter(item.values()))
            key = (record['startTime'], record['endTime'])
            if key in seen:
                raise ValueError('Repeated archive page')
            seen.add(key)
            if key == (start, end):
                found = True
        if len(page) < 100:
            break
        index += len(page)
    if not found:
        raise RecordingUnavailable()


async def download(job):
    from camera import camera_settings
    from pytapo import Tapo
    from pytapo.media_stream.downloader import Downloader
    from pytapo.media_stream._utils import StreamType
    importlib.import_module('pytapo.transport.pytapo.pytapo').MAX_LOGIN_RETRIES = 0
    values = {}
    for line in Path(job['env']).read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            values[k.strip()] = v.strip().strip('\"\'')
    host = camera_settings(Path(job['env']))[0]
    if host != job['camera_host']:
        raise ValueError('Camera changed')
    password = values['TAPO_CLOUD_PASSWORD']
    job['stage'] = 'camera_connection'
    tapo = await asyncio.to_thread(Tapo, host, 'admin', password, password,
                                 retryStok=False, printDebugInformation=False, printWarnInformation=False)
    await asyncio.to_thread(tapo.getUserID)
    # Initialise the SD playback search for this session before requesting media.
    job['stage'] = 'camera_archive'
    date = datetime.fromtimestamp(job['start_epoch'], ZoneInfo('Europe/Warsaw')).strftime('%Y%m%d')
    await require_recording(tapo, date, job['start_epoch'], job['end_epoch'])
    job['stage'] = 'camera_download'
    session = await asyncio.to_thread(tapo.getMediaSession, StreamType.Download)
    tapo.getMediaSession = lambda *_: session
    target = Path(job['target'])
    target.parent.mkdir(parents=True, exist_ok=True)
    downloader = Downloader(tapo, job['start_epoch'], job['end_epoch'],
                            job.get('clock_offset_seconds') or 0,
                            outputDirectory=str(target.parent) + '/', fileName=target.name,
                            padding=0, window_size=50, stall_timeout=25, overwriteFiles=True, progressInterval=10)
    async for status in downloader.download():
        job["last_action"] = status.get("currentAction")
        job["downloaded_seconds"] = status.get("progress")
    # A deletion during transfer can make the camera skip to the next clip.
    # Reject that transfer before the caller can publish or analyse it.
    try:
        await require_recording(tapo, date, job['start_epoch'], job['end_epoch'])
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if not target.is_file() or target.stat().st_size == 0:
        raise FileNotFoundError('No media')


if __name__ == '__main__':
    job = {}
    try:
        job = json.load(sys.stdin)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            asyncio.run(asyncio.wait_for(download(job), timeout=420))
        print('{"ok":true}')
    except Exception as error:
        # Only controlled codes cross the process boundary; library errors can contain secrets.
        print(json.dumps({'ok': False, 'error_stage': job.get('stage', 'camera_download'),
                          'error_code': 'recording_missing' if isinstance(error, RecordingUnavailable) else 'timeout' if isinstance(error, TimeoutError) else
                          'no_media' if isinstance(error, FileNotFoundError) else 'camera_error'}))
        sys.exit(1)
