"""Local litter-box monitor. Run: python3 server.py (Python 3.10+)."""
import argparse
import csv
import hashlib
import io
import json
import math
import os
import queue
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
for line in (ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else []:
    if line.strip() and not line.lstrip().startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
from qwen import configuration
from camera import CameraPreview
from recordings import RecordingIndex, RecordingMonitor
from analysis_pipeline import AnalysisPipeline, join_visit_segments

CATS = [{"id": "kefir", "name": "Kefir", "description": "Masywny tułów, mniej puszysty ogon", "color": "sage"},
        {"id": "kalinka", "name": "Kalinka", "description": "Smukła sylwetka, duży puszysty ogon", "color": "peach"}]
REGIONS = ["lewy tył", "środek z tyłu", "prawy tył", "lewy środek", "środek",
           "prawy środek", "lewy przód", "środek z przodu", "prawy przód"]
OUTCOMES = {"urine": "Prawdopodobny mocz", "feces": "Prawdopodobny kał",
            "both": "Prawdopodobny mocz i kał", "empty": "Nie wykryto oznak moczu ani kału",
            "uncertain": "Nie można ocenić", "confirmed": "Mocz potwierdzony ręcznie",
            "feces_confirmed": "Kał potwierdzony ręcznie", "both_confirmed": "Mocz i kał potwierdzone ręcznie"}
URINE_OUTCOMES = {"urine", "both", "confirmed", "both_confirmed"}
FECES_OUTCOMES = {"feces", "both", "feces_confirmed", "both_confirmed"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path, seed=True):
        self.lock = threading.RLock()
        self.listeners = set()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS boxes (id INTEGER PRIMARY KEY, name TEXT NOT NULL, cleaned_at TEXT);
            CREATE TABLE IF NOT EXISTS visits (
                id TEXT PRIMARY KEY, cat_id TEXT NOT NULL, box_id INTEGER NOT NULL,
                entered_at TEXT NOT NULL, exited_at TEXT, status TEXT NOT NULL,
                outcome TEXT, region INTEGER, scenario TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                original_outcome TEXT, source TEXT NOT NULL DEFAULT 'mock', reviewed_at TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS active_cat ON visits(cat_id) WHERE status='active';
            CREATE UNIQUE INDEX IF NOT EXISTS active_box ON visits(box_id) WHERE status='active';
            CREATE TABLE IF NOT EXISTS notices (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                visit_id TEXT, text TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY, visit_id TEXT NOT NULL,
                created_at TEXT NOT NULL, previous_outcome TEXT, outcome TEXT NOT NULL, note TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tail_labels (
                visit_id TEXT NOT NULL, file TEXT NOT NULL, image_sha TEXT NOT NULL,
                point TEXT NOT NULL, model_point TEXT, frame_time REAL, updated_at TEXT NOT NULL,
                PRIMARY KEY (visit_id, file));
            INSERT OR IGNORE INTO boxes VALUES (1, 'Kuweta 01', NULL), (2, 'Kuweta 02', NULL);
        """)
        if "feces_region" not in {row[1] for row in self.db.execute("PRAGMA table_info(visits)")}:
            self.db.execute("ALTER TABLE visits ADD COLUMN feces_region INTEGER")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(visits)")}
        for column in ('urine_point', 'feces_point', 'continuation_of'):
            if column not in columns:
                self.db.execute(f"ALTER TABLE visits ADD COLUMN {column} TEXT")
        # Never turn an interrupted simulation into a successful result after restart.
        interrupted = self.db.execute("SELECT id FROM visits WHERE status='active'").fetchall()
        for row in interrupted:
            self.db.execute("UPDATE visits SET status='done', exited_at=?, outcome='uncertain', "
                            "original_outcome='uncertain', region=NULL, feces_region=NULL, note=? WHERE id=?",
                            (now(), "Symulacja przerwana przez restart aplikacji.", row["id"]))
            self.notice(row["id"], "Symulacja przerwana — wynik nieznany po restarcie.")
        if seed and not self.db.execute("SELECT 1 FROM metadata WHERE key='seeded'").fetchone():
            for minutes, cat, box, outcome, region, duration in [
                (155, "kefir", 1, "urine", 2, 74), (102, "kalinka", 2, "urine", 6, 52),
                (46, "kefir", 2, "empty", None, 18), (12, "kalinka", 1, "uncertain", None, 39)]:
                start = datetime.now(timezone.utc) - timedelta(minutes=minutes)
                self.db.execute("INSERT INTO visits (id,cat_id,box_id,entered_at,exited_at,status,outcome,"
                                "region,scenario,original_outcome) VALUES (?,?,?,?,?,'done',?,?,?,?)",
                                (uuid.uuid4().hex, cat, box, start.isoformat(),
                                 (start + timedelta(seconds=duration)).isoformat(), outcome, region, outcome, outcome))
            self.db.execute("INSERT INTO metadata VALUES ('seeded','1')")
            self.notice(None, "Wczytano 4 przykładowe wizyty. Wszystkie dane są symulowane.")
        self.db.commit()
        self.recordings = RecordingIndex(self)

    def notice(self, visit_id, text):
        self.db.execute("INSERT INTO notices (created_at,visit_id,text) VALUES (?,?,?)", (now(), visit_id, text))

    def tail_frames(self, visit_id):
        """Manual tail landmarks are separate from model output and waste reviews."""
        match = re.fullmatch(r'([a-f0-9]{24})-(\d+)', visit_id)
        with self.lock:
            if not match or not self.db.execute(
                    "SELECT 1 FROM visits WHERE id=? AND source='camera'", (visit_id,)).fetchone():
                raise ValueError("Nie znaleziono wizyty z kamery.")
            row = self.db.execute('SELECT analysis FROM camera_recordings WHERE media_key=?',
                                  (match[1],)).fetchone()
            visits = json.loads(row['analysis']).get('visits', []) if row and row['analysis'] else []
            index = int(match[2])
            checks = visits[index].get('focus', {}).get('pose_checks', []) if index < len(visits) else []
            frames = []
            for check in checks:
                name = check.get('file', '')
                if not re.fullmatch(r'visit-\d+-anatomy-\d+\.jpg', name):
                    continue
                image = ROOT / 'data' / 'recordings' / 'processed' / match[1] / name
                if not image.is_file():
                    continue
                digest = hashlib.sha256(image.read_bytes()).hexdigest()
                label = self.db.execute('SELECT point,updated_at FROM tail_labels '
                                        'WHERE visit_id=? AND file=? AND image_sha=?',
                                        (visit_id, name, digest)).fetchone()
                frames.append({'file': name, 't': check.get('t'), 'image_sha': digest,
                               'model_point': check.get('tail_base_point'),
                               'point': json.loads(label['point']) if label else None,
                               'updated_at': label['updated_at'] if label else None})
            return {'media_key': match[1], 'frames': frames}

    def save_tail_label(self, visit_id, data):
        point = data.get('point')
        if point is not None and (not isinstance(point, list) or len(point) != 2 or any(
                type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 1 for n in point)):
            raise ValueError("Wskaż prawidłowy punkt na zdjęciu.")
        with self.lock:
            frame = next((f for f in self.tail_frames(visit_id)['frames'] if f['file'] == data.get('file')), None)
            if not frame or frame['image_sha'] != data.get('image_sha'):
                raise ValueError("Klatka zmieniła się lub jest niedostępna. Otwórz wizytę ponownie.")
            with self.db:
                if point is None:
                    self.db.execute('DELETE FROM tail_labels WHERE visit_id=? AND file=?', (visit_id, frame['file']))
                else:
                    self.db.execute('INSERT INTO tail_labels VALUES (?,?,?,?,?,?,?) '
                                    'ON CONFLICT(visit_id,file) DO UPDATE SET image_sha=excluded.image_sha, '
                                    'point=excluded.point,model_point=excluded.model_point, '
                                    'frame_time=excluded.frame_time,updated_at=excluded.updated_at',
                                    (visit_id, frame['file'], frame['image_sha'], json.dumps(point),
                                     json.dumps(frame['model_point']), frame['t'], now()))
        return {'ok': True}

    def camera_regions(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM metadata WHERE key='camera_regions'").fetchone()
            return json.loads(row[0]) if row else {"boxes": [], "updated_at": None}

    def save_camera_regions(self, data):
        boxes = data.get("boxes")
        if not isinstance(boxes, list) or len(boxes) != 2:
            raise ValueError("Zaznacz obie kuwety.")
        cleaned = []
        for box in boxes:
            if not isinstance(box, dict) or type(box.get("id")) is not int or box["id"] not in (1, 2):
                raise ValueError("Nieprawidłowa kuweta.")
            points = box.get("points")
            if not isinstance(points, list) or len(points) != 4:
                raise ValueError("Zaznacz cztery narożniki każdej kuwety.")
            for point in points:
                if (not isinstance(point, list) or len(point) != 2 or
                    any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in point)):
                    raise ValueError("Punkty muszą znajdować się w obrazie.")
            turns = []
            for i in range(4):
                a, b, c = points[i], points[(i + 1) % 4], points[(i + 2) % 4]
                turns.append((b[0]-a[0])*(c[1]-b[1]) - (b[1]-a[1])*(c[0]-b[0]))
            area = abs(sum(points[i][0]*points[(i+1)%4][1] - points[(i+1)%4][0]*points[i][1] for i in range(4))) / 2
            if area < 0.001 or not (all(t > 0 for t in turns) or all(t < 0 for t in turns)):
                raise ValueError("Zaznacz narożniki kolejno dookoła kuwety, bez przecinania boków.")
            cleaned.append({"id": box["id"], "points": points})
        if {box["id"] for box in cleaned} != {1, 2}:
            raise ValueError("Wymagane są dwie różne kuwety.")
        result = {"boxes": sorted(cleaned, key=lambda box: box["id"]), "updated_at": now()}
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('camera_regions', ?)", (json.dumps(result),))
            self.db.commit()
        return result

    def snapshot(self):
        with self.lock:
            visits = [dict(x) for x in self.db.execute("SELECT * FROM visits ORDER BY entered_at DESC")]
            for visit in visits:
                for field in ('urine_point', 'feces_point'):
                    visit[field] = json.loads(visit[field]) if visit[field] else None
            return {"cats": CATS, "regions": REGIONS, "outcomes": OUTCOMES, "mode": "camera",
                    "qwen": configuration(), "server_time": now(), "recordings": self.recordings.snapshot(),
                    "boxes": [dict(x) for x in self.db.execute("SELECT * FROM boxes ORDER BY id")],
                    "visits": join_visit_segments(visits),
                    "notices": [dict(x) for x in self.db.execute("SELECT * FROM notices ORDER BY id DESC LIMIT 40")]}

    def publish(self):
        with self.lock:
            for listener in list(self.listeners):
                try:
                    listener.put_nowait(True)
                except queue.Full:
                    pass

    def start(self, data):
        cat, box, scenario, region = (data.get(k) for k in ("cat_id", "box_id", "scenario", "region"))
        if cat not in {x["id"] for x in CATS} or type(box) is not int or box not in (1, 2):
            raise ValueError("Wybierz kota i kuwetę.")
        if scenario not in ("urine", "feces", "both", "empty", "uncertain"):
            raise ValueError("Nieprawidłowy scenariusz.")
        if type(region) is not int or region not in range(9):
            raise ValueError("Wybierz pole kuwety.")
        feces_region = data.get("feces_region")
        if scenario in FECES_OUTCOMES:
            if type(feces_region) is not int or feces_region not in range(9):
                raise ValueError("Wybierz miejsce kału.")
        else:
            feces_region = None
        visit_id = uuid.uuid4().hex
        with self.lock, self.db:
            try:
                self.db.execute("INSERT INTO visits (id,cat_id,box_id,entered_at,status,scenario,region,feces_region) "
                                "VALUES (?,?,?,?,'active',?,?,?)",
                                (visit_id, cat, box, now(), scenario, region, feces_region))
            except sqlite3.IntegrityError:
                raise ValueError("Ten kot lub ta kuweta uczestniczy już w trwającej wizycie.") from None
            self.notice(visit_id, f"{cat.capitalize()} wszedł do kuwety {box}. [Symulacja]")
        self.publish()
        return visit_id

    def finish(self, visit_id):
        with self.lock, self.db:
            visit = self.db.execute("SELECT * FROM visits WHERE id=? AND status='active'", (visit_id,)).fetchone()
            if not visit:
                return
            outcome = visit["scenario"]
            region = visit["region"] if outcome in URINE_OUTCOMES else None
            self.db.execute("UPDATE visits SET status='done',exited_at=?,outcome=?,original_outcome=?,region=? "
                            "WHERE id=?", (now(), outcome, outcome, region, visit_id))
            location = f" Mocz — miejsce: {REGIONS[region]}." if region is not None else ""
            if visit["feces_region"] is not None:
                location += f" Kał — miejsce: {REGIONS[visit['feces_region']]}."
            self.notice(visit_id, f"{visit['cat_id'].capitalize()} wyszedł z kuwety {visit['box_id']}. "
                        f"{OUTCOMES[outcome]}.{location} [Symulacja]")
        self.publish()

    def review(self, visit_id, data):
        outcome, note = data.get("outcome"), data.get("note", "")
        if outcome not in OUTCOMES or not isinstance(note, str) or len(note) > 1000:
            raise ValueError("Nieprawidłowy wynik lub notatka (maksymalnie 1000 znaków).")
        regions = {field: data.get(field) for field in ('region', 'feces_region')}
        points = {}
        for value in regions.values():
            if value is not None and (type(value) is not int or value not in range(9)):
                raise ValueError("Nieprawidłowe miejsce.")
        for field, point_field in (('region', 'urine_point'), ('feces_region', 'feces_point')):
            point = data.get(point_field)
            if point is not None:
                if not isinstance(point, list) or len(point) != 2 or any(
                        type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 1 for n in point):
                    raise ValueError("Punkt na zdjęciu musi mieć współrzędne x/y od 0 do 1.")
                region = min(2, int(point[1]*3))*3 + min(2, int(point[0]*3))
                if regions[field] is not None and regions[field] != region:
                    raise ValueError("Punkt i obszar zdjęcia nie są zgodne.")
                regions[field] = region
            points[point_field] = json.dumps(point) if point is not None else None
        with self.lock, self.db:
            visit = self.db.execute("SELECT * FROM visits WHERE id=?", (visit_id,)).fetchone()
            if not visit or visit["status"] != "done":
                raise ValueError("Można oceniać tylko zakończone wizyty.")
            cat = data.get('cat_id', visit['cat_id'])
            if cat not in ('kefir', 'kalinka', 'unknown'):
                raise ValueError('Nieprawidłowy kot.')
            for field, point_field, outcomes in (('region', 'urine_point', URINE_OUTCOMES),
                                                 ('feces_region', 'feces_point', FECES_OUTCOMES)):
                if outcome not in outcomes:
                    regions[field], points[point_field] = None, None
                elif point_field not in data and regions[field] == visit[field]:
                    points[point_field] = visit[point_field]
            session = next((v for v in join_visit_segments(
                [dict(r) for r in self.db.execute("SELECT * FROM visits")]) if v['id'] == visit_id), None)
            if session and session.get('segments'):
                for part in session['segments']:
                    self.db.execute("UPDATE visits SET cat_id=? WHERE id=?", (cat, part['id']))
            self.db.execute("INSERT INTO reviews (visit_id,created_at,previous_outcome,outcome,note) VALUES (?,?,?,?,?)",
                            (visit_id, now(), visit["outcome"], outcome, note))
            self.db.execute("UPDATE visits SET outcome=?,note=?,region=?,feces_region=?,urine_point=?,feces_point=?,"
                            "reviewed_at=?,cat_id=? WHERE id=?",
                            (outcome, note, regions['region'], regions['feces_region'], points['urine_point'],
                             points['feces_point'], now(), cat, visit_id))
            self.notice(visit_id, f"Zapisano ocenę wizyty: {OUTCOMES[outcome]}." + (' [Dane testowe]' if visit['source'] == 'mock' else ''))
        self.publish()

    def clean(self, box_id):
        if box_id not in (1, 2):
            raise ValueError("Nieprawidłowa kuweta.")
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM visits WHERE box_id=? AND status='active'", (box_id,)).fetchone():
                raise ValueError("Poczekaj na zakończenie wizyty.")
            self.db.execute("UPDATE boxes SET cleaned_at=? WHERE id=?", (now(), box_id))
            self.notice(None, f"Kuweta {box_id}: oznaczono sprzątanie. Historia pozostaje zachowana. [Symulacja]")
        self.publish()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def send(self, status, content, mime="application/json; charset=utf-8", extra=None):
        if not isinstance(content, bytes):
            content = json.dumps(content, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; "
                         "script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        path = urlparse(self.path).path
        tail = re.fullmatch(r'/api/visits/([^/]+)/tail-labels', path)
        if tail:
            try:
                return self.send(200, self.server.store.tail_frames(tail[1]))
            except ValueError as error:
                return self.send(400, {'error': str(error)})
        evidence = re.fullmatch(r'/api/recordings/([a-f0-9]{24})/evidence/(visit-\d+-(?:(?:urine|feces)(?:-before)?|(?:posture|anatomy)-\d+)\.jpg)', path)
        if evidence:
            with self.server.store.lock:
                row = self.server.store.db.execute('SELECT analysis FROM camera_recordings WHERE media_key=?', (evidence[1],)).fetchone()
            analysis = json.loads(row['analysis']) if row and row['analysis'] else {}
            allowed = {e.get(field) for v in analysis.get('visits', [])
                       for e in v.get('focus', {}).get('evidence', []) for field in ('file', 'before_file')}
            allowed.update(s.get('file') for v in analysis.get('visits', [])
                           for s in v.get('focus', {}).get('suggestions', []))
            allowed.update(s.get('file') for v in analysis.get('visits', [])
                           for s in v.get('focus', {}).get('pose_checks', []))
            file = ROOT / 'data' / 'recordings' / 'processed' / evidence[1] / evidence[2]
            if evidence[2] not in allowed or not file.is_file():
                return self.send(404, {'error': 'Brak klatki dowodowej.'})
            return self.send(200, file.read_bytes(), 'image/jpeg')
        media = re.fullmatch(r'/api/recordings/([a-f0-9]{24})/video', path)
        if media:
            with self.server.store.lock:
                exists = self.server.store.db.execute('SELECT 1 FROM camera_recordings WHERE media_key=?', (media[1],)).fetchone()
            file = ROOT / 'data' / 'recordings' / 'processed' / media[1] / 'recording.mp4'
            if not exists or not file.is_file():
                return self.send(404, {'error': 'Brak nagrania.'})
            size = file.stat().st_size
            start, end = 0, size - 1
            requested = self.headers.get('Range')
            if requested:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
                if not match or not any(match.groups()):
                    return self.send(416, {}, extra={'Content-Range': f'bytes */{size}'})
                if match[1]:
                    start = int(match[1])
                    end = min(end, int(match[2])) if match[2] else end
                else:
                    start = max(0, size - int(match[2]))
                if start > end or start >= size:
                    return self.send(416, {}, extra={'Content-Range': f'bytes */{size}'})
            self.send_response(206 if requested else 200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'no-store')
            if requested:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            try:
                with file.open('rb') as stream:
                    stream.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = stream.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path == "/api/camera/regions":
            return self.send(200, self.server.store.camera_regions())
        if path in ("/api/camera/status", "/api/camera/frame.jpg"):
            camera = self.server.camera
            status, frame = camera.snapshot() if camera else (
                {"online": False, "message": "Podgląd kamery nie jest uruchomiony.", "captured_at": None}, None)
            if path.endswith("status"):
                return self.send(200, status)
            if frame is None:
                return self.send(503, {"error": status["message"]})
            return self.send(200, frame, "image/jpeg", {"X-Captured-At": status["captured_at"]})
        if path == "/api/state":
            return self.send(200, self.server.store.snapshot())
        if path == "/api/events":
            return self.events()
        if path == "/api/export.csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["Źródło", "Kot", "Kuweta", "Wejście UTC", "Wyjście UTC", "Wynik", "Miejsce moczu", "Miejsce kału", "Notatka"])
            for v in self.server.store.snapshot()["visits"]:
                note = v["note"]
                if note.lstrip().startswith(("=", "+", "-", "@")) or note.startswith(("\t", "\r", "\n")):
                    note = "'" + note
                writer.writerow(["SYMULACJA" if v['source'] == 'mock' else 'KAMERA — czas obserwowany, przybliżony', v["cat_id"], v["box_id"], v["entered_at"], v["exited_at"],
                                 OUTCOMES.get(v["outcome"], "Wizyta trwa"),
                                 REGIONS[v["region"]] if v["region"] is not None else "",
                                 REGIONS[v["feces_region"]] if v["feces_region"] is not None else "", note])
            return self.send(200, output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
                             {"Content-Disposition": 'attachment; filename="kuweta-wizyty.csv"'})
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8"),
                  "/favicon.svg": ("favicon.svg", "image/svg+xml")}
        if path in assets:
            file, mime = assets[path]
            return self.send(200, (ROOT / "web" / file).read_bytes(), mime)
        self.send(404, {"error": "Nie znaleziono."})

    def events(self):
        listener = queue.Queue(maxsize=1)
        with self.server.store.lock:
            self.server.store.listeners.add(listener)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                payload = json.dumps(self.server.store.snapshot(), ensure_ascii=False)
                self.wfile.write(f"event: state\ndata: {payload}\n\n".encode())
                self.wfile.flush()
                try:
                    listener.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self.server.store.lock:
                self.server.store.listeners.discard(listener)

    def do_POST(self):
        try:
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self.send(403, {"error": "Niedozwolone źródło żądania."})
            if self.headers.get_content_type() != "application/json":
                return self.send(415, {"error": "Wymagany JSON."})
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 8192:
                return self.send(413, {"error": "Zbyt duże żądanie."})
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("Wymagany obiekt JSON.")
            path = urlparse(self.path).path
            store = self.server.store
            tail = re.fullmatch(r'/api/visits/([^/]+)/tail-labels', path)
            if tail:
                return self.send(200, store.save_tail_label(tail[1], data))
            if path == "/api/camera/regions":
                return self.send(200, store.save_camera_regions(data))
            if path == "/api/simulate":
                visit_id = store.start(data)
                timer = threading.Timer(self.server.simulation_seconds, store.finish, args=(visit_id,))
                timer.daemon = True
                timer.start()
                return self.send(201, {"visit_id": visit_id})
            if path.startswith("/api/visits/") and path.endswith("/review"):
                store.review(path.split("/")[3], data)
                return self.send(200, {"ok": True})
            if path.startswith("/api/boxes/") and path.endswith("/clean"):
                store.clean(int(path.split("/")[3]))
                return self.send(200, {"ok": True})
            self.send(404, {"error": "Nie znaleziono."})
        except (ValueError, json.JSONDecodeError) as error:
            self.send(400, {"error": str(error)})
        except Exception:
            import traceback
            traceback.print_exc()
            self.send(500, {"error": "Nie udało się zapisać danych. Spróbuj ponownie."})


def create_server(path, host="127.0.0.1", port=8765, seed=True, simulation_seconds=8):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.store = Store(path, seed)
    server.simulation_seconds = simulation_seconds
    server.camera = None
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--db", default=str(ROOT / "data" / "kuweta.sqlite3"))
    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    server = create_server(args.db, args.host, args.port)
    server.camera = CameraPreview(ROOT / ".env")
    server.camera.start()
    recording_monitor = RecordingMonitor(server.store, ROOT)
    recording_monitor.start()
    analysis_pipeline = AnalysisPipeline(server.store, ROOT)
    analysis_pipeline.start()
    print(f"Kuweta · http://{args.host}:{server.server_port} · analiza nagrań aktywna", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        analysis_pipeline.close()
        recording_monitor.close()
        server.camera.close()
        server.server_close()
