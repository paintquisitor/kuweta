"""Persistent camera recording inventory; recordings are not confirmed cat visits."""
import json
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


class RecordingIndex:
    def __init__(self, store):
        self.store = store
        store.db.execute("""CREATE TABLE IF NOT EXISTS camera_recordings (
            camera_host TEXT NOT NULL, start_epoch INTEGER NOT NULL, end_epoch INTEGER NOT NULL,
            started_at TEXT NOT NULL, ended_at TEXT NOT NULL, detected_at TEXT NOT NULL,
            status TEXT NOT NULL, camera_type INTEGER, clock_offset_seconds INTEGER,
            notified INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (camera_host, start_epoch))""")
        store.db.commit()
        columns = {r[1] for r in store.db.execute('PRAGMA table_info(camera_recordings)')}
        for name, definition in {'attempts': 'INTEGER NOT NULL DEFAULT 0',
                                 'retry_at': 'REAL NOT NULL DEFAULT 0',
                                 'error': 'TEXT', 'media_key': 'TEXT', 'analysis': 'TEXT'}.items():
            if name not in columns:
                store.db.execute(f'ALTER TABLE camera_recordings ADD COLUMN {name} {definition}')
        store.db.commit()

    def snapshot(self):
        with self.store.lock:
            row = self.store.db.execute("SELECT value FROM metadata WHERE key='recording_monitor'").fetchone()
            status = json.loads(row[0]) if row else {}
            status["interval_seconds"] = 60
            status["items"] = [dict(row) for row in self.store.db.execute(
                "SELECT * FROM camera_recordings ORDER BY start_epoch DESC LIMIT 100")]
            return status

    def ingest(self, result, checked_at=None):
        checked_at = time.time() if checked_at is None else checked_at
        host = result["camera_host"]
        records = result["recordings"]
        # Validate the whole response before committing either records or the poll cursor.
        for record in records:
            start, end = record["startTime"], record["endTime"]
            if type(start) is not int or type(end) is not int or start <= 0 or end <= start:
                raise ValueError("Nieprawidłowy czas nagrania")
            iso(start), iso(end)
        with self.store.lock, self.store.db:
            for record in records:
                start, end = record["startTime"], record["endTime"]
                old = self.store.db.execute(
                    "SELECT * FROM camera_recordings WHERE camera_host=? AND start_epoch=?", (host, start)).fetchone()
                end = max(end, old["end_epoch"]) if old else end
                status = "recording" if end > checked_at - 60 else "pending_analysis"
                if old and old["status"] not in ("recording", "pending_analysis"):
                    status = old["status"]
                    if end > old["end_epoch"] and status == "waiting_model":
                        status = "recording" if end > checked_at - 60 else "pending_analysis"
                    elif end > old["end_epoch"] and status not in ("ignored_test", "downloading", "analyzing"):
                        status = "needs_review"
                self.store.db.execute("""INSERT INTO camera_recordings
                    (camera_host,start_epoch,end_epoch,started_at,ended_at,detected_at,status,camera_type,clock_offset_seconds)
                    VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(camera_host,start_epoch) DO UPDATE SET
                    end_epoch=excluded.end_epoch,ended_at=excluded.ended_at,status=excluded.status,
                    clock_offset_seconds=excluded.clock_offset_seconds""",
                    (host, start, end, iso(start), iso(end), iso(checked_at), status,
                     record.get("vedio_type"), result.get("clock_offset_seconds")))
                if status == "pending_analysis" and (not old or not old["notified"]):
                    local = datetime.fromtimestamp(start, ZoneInfo("Europe/Warsaw")).strftime("%d.%m %H:%M:%S")
                    self.store.notice(None, f"Nowe nagranie kamery: {local}, {end-start} s. Oczekuje pobrania i analizy; kot niepotwierdzony.")
                    self.store.db.execute("UPDATE camera_recordings SET notified=1 WHERE camera_host=? AND start_epoch=?", (host, start))
            self._status({"camera_host": host, "cursor_epoch": result["checked_through"],
                          "last_checked_at": iso(checked_at), "last_attempt_at": iso(checked_at),
                          "clock_offset_seconds": result.get("clock_offset_seconds"), "error": None})
        self.store.publish()

    def _status(self, status):
        self.store.db.execute("INSERT OR REPLACE INTO metadata VALUES ('recording_monitor',?)", (json.dumps(status),))

    def failed(self, message):
        with self.store.lock, self.store.db:
            previous = self.snapshot()
            previous.pop("items", None)
            previous.update(error=message, last_attempt_at=iso(time.time()))
            self._status(previous)
        self.store.publish()


class RecordingMonitor:
    def __init__(self, store, root):
        self.index = store.recordings
        self.root = Path(root)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=55)

    def run(self):
        while not self.stop.is_set():
            delay = 60
            try:
                status = self.index.snapshot()
                since = status.get("cursor_epoch", time.time() - 86400)
                process = subprocess.run(
                    [str(self.root / ".venv-tapo/bin/python"), str(self.root / "recordings.py"),
                     str(self.root / ".env"), str(int(since)), status.get("camera_host", "")],
                    capture_output=True, text=True, timeout=50)
                result = json.loads(process.stdout)
                if process.returncode or result.get("error"):
                    raise ValueError(result.get("error", "Nie udało się odczytać listy nagrań."))
                self.index.ingest(result)
                if result.get("more_days"):
                    delay = 1
            except FileNotFoundError:
                self.index.failed("Brak środowiska .venv-tapo. Sprawdzanie nagrań nie działa.")
                delay = 300
            except Exception as error:
                message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else "Brak odpowiedzi kamery podczas sprawdzania nagrań. Ponowienie za 5 minut."
                self.index.failed(message)
                delay = 300
            self.stop.wait(delay)


def query(env_path, since, previous_host):
    # Import only in the isolated worker; the web app needs no Tapo dependencies.
    import importlib
    from pytapo import Tapo
    importlib.import_module("pytapo.transport.pytapo.pytapo").MAX_LOGIN_RETRIES = 0
    values = {}
    for line in Path(env_path).read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    host, password = values.get("CAMERA_HOST", ""), values.get("TAPO_CLOUD_PASSWORD", "")
    if not host or not password:
        return {"error": "Uzupełnij CAMERA_HOST i TAPO_CLOUD_PASSWORD w .env."}
    current = int(time.time())
    if previous_host != host:
        since = current - 86400
    tapo = Tapo(host, "admin", password, password, retryStok=False,
                printDebugInformation=False, printWarnInformation=False)
    zone = ZoneInfo("Europe/Warsaw")
    first = datetime.fromtimestamp(since, zone).date() - timedelta(days=1)
    today = datetime.fromtimestamp(current, zone).date()
    last = min(first + timedelta(days=2), today)
    recordings = {}
    day = first
    while day <= last:
        # The tested camera returns recordings via date search, but not UTC search.
        index = 0
        seen = set()
        while True:
            page = tapo.getRecordings(day.strftime("%Y%m%d"), start_index=index, end_index=index + 99)
            for item in page:
                record = next(iter(item.values()))
                key = (record["startTime"], record["endTime"])
                if key in seen:
                    raise ValueError("Repeated recording page")
                seen.add(key)
                recordings[record["startTime"]] = record
            if len(page) < 100:
                break
            index += len(page)
        day += timedelta(days=1)
    more = last < today
    through = int(datetime.combine(last + timedelta(days=1), datetime.min.time(), zone).timestamp()) - 1 if more else current
    offset = tapo.getTimeCorrection()
    return {"camera_host": host, "recordings": list(recordings.values()),
            "checked_through": through, "more_days": more,
            "clock_offset_seconds": offset if type(offset) is int else None}


if __name__ == "__main__":
    import contextlib
    import io
    import sys
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = query(sys.argv[1], int(sys.argv[2]), sys.argv[3])
    except Exception:
        # Library exceptions can include authentication payloads: never expose them.
        result = {"error": "Nie udało się odczytać archiwum Tapo. Sprawdź połączenie, hasło i zgodność z aplikacjami innych firm. Ponowienie za 5 minut."}
    print(json.dumps(result))
