"""Local RTSP preview. Credentials and raw decoder errors never leave this module."""
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def camera_settings(env_path):
    keys = ("CAMERA_HOST", "CAMERA_USERNAME", "CAMERA_PASSWORD")
    values = {key: os.getenv(key, "") for key in keys}
    if Path(env_path).exists():
        for line in Path(env_path).read_text().splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            if key.strip() in values:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    return tuple(values[key] for key in keys)


def stream_url(settings):
    host, username, password = settings
    # This field is a camera address, never a URL containing credentials or paths.
    if not host or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in host):
        raise ValueError("Nieprawidłowy CAMERA_HOST.")
    return f"rtsp://{quote(username, safe='')}:{quote(password, safe='')}@{host}:554/stream1"


class CameraPreview:
    def __init__(self, env_path):
        self.env_path = env_path
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.process = None
        self.thread = None
        self.frame = None
        self.received = 0
        self.captured_at = None
        self.message = "Łączenie z kamerą…"

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def snapshot(self):
        with self.lock:
            online = self.frame is not None and time.monotonic() - self.received < 10
            return ({"online": online,
                     "message": "Podgląd połączony" if online else self.message,
                     "captured_at": self.captured_at}, self.frame if online else None)

    def _offline(self, message):
        with self.lock:
            self.frame = None
            self.message = message

    def _run(self):
        rejected = None
        while not self.stop_event.is_set():
            try:
                settings = camera_settings(self.env_path)
                if not all(settings):
                    self._offline("Uzupełnij dane konta kamery w pliku .env.")
                    self.stop_event.wait(2)
                    continue
                if settings == rejected:
                    self.stop_event.wait(2)
                    continue
                executable = shutil.which("ffmpeg")
                if not executable:
                    self._offline("Brak FFmpeg na komputerze z aplikacją.")
                    self.stop_event.wait(10)
                    continue
                url = stream_url(settings)
                self._offline("Łączenie z kamerą…")
                with self.lock:
                    if self.stop_event.is_set():
                        break
                    self.process = subprocess.Popen(
                        [executable, "-nostdin", "-hide_banner", "-loglevel", "error",
                         "-rtsp_transport", "tcp", "-timeout", "8000000", "-i", url,
                         "-map", "0:v:0", "-an", "-vf", "fps=1,scale=1280:-2",
                         "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    process = self.process
                auth_failed = threading.Event()

                def read_errors():
                    while line := process.stderr.readline():
                        if b"401" in line or b"403" in line:
                            auth_failed.set()

                errors = threading.Thread(target=read_errors, daemon=True)
                errors.start()
                buffer = b""
                try:
                    while not self.stop_event.is_set():
                        chunk = process.stdout.read1(65536)
                        if not chunk:
                            break
                        buffer += chunk
                        while b"\xff\xd9" in buffer:
                            end = buffer.index(b"\xff\xd9") + 2
                            start = buffer.find(b"\xff\xd8", 0, end)
                            if start >= 0:
                                with self.lock:
                                    self.frame = buffer[start:end]
                                    self.received = time.monotonic()
                                    self.captured_at = datetime.now(timezone.utc).isoformat()
                                    self.message = "Brak aktualnego obrazu. Ponowne łączenie…"
                            buffer = buffer[end:]
                        if len(buffer) > 4_000_000:
                            break
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    errors.join(timeout=2)
                    process.stdout.close()
                    process.stderr.close()
                    with self.lock:
                        self.process = None
                if auth_failed.is_set():
                    rejected = settings
                    self._offline("Kamera odrzuciła login lub hasło. Popraw konto kamery w .env.")
                else:
                    self._offline("Brak połączenia z kamerą. Ponowna próba za chwilę…")
            except (OSError, ValueError):
                self._offline("Nie można uruchomić podglądu. Sprawdź konfigurację kamery i FFmpeg.")
            self.stop_event.wait(3)

    def close(self):
        self.stop_event.set()
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
        if self.thread is not None:
            self.thread.join(timeout=5)
