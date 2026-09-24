"""Local recording queue and conservative, sampled vision analysis."""
import hashlib
import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError

from qwen import ModelUnavailable, analyze_images
from recordings import iso
from focus_analysis import bounds, inspect_focus
from cat_identity import identify_visit
from runtime_paths import recordings_path

STEP = 2  # Seconds; observed intervals are approximate, never exact entry/exit.


class ProcessingError(Exception):
    """A controlled, public message; never construct from raw library errors."""


def download_error(reply):
    if reply.get('error_code') == 'timeout':
        return 'Przekroczono czas oczekiwania podczas pobierania z kamery. Qwen nie analizował filmu.'
    return {
        'camera_connection': 'Nie udało się połączyć lub zalogować do kamery. Qwen nie analizował filmu.',
        'camera_archive': 'Nie udało się odczytać archiwum kamery. Qwen nie analizował filmu.',
    }.get(reply.get('error_stage'),
          'Nie udało się pobrać filmu z kamery. Brak poprawnej lokalnej kopii; Qwen nie analizował filmu.')


def structured(images, context, *, task='litter'):
    text = analyze_images(images, context, json_output=True, task=task).strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        # Qwen sometimes appends a single stray quote to an otherwise complete
        # JSON object. Accept only this narrow case, never a second response.
        result, end = json.JSONDecoder().raw_decode(text)
        if text[end:].strip() != '"':
            raise ValueError('Invalid model response') from None
    if not isinstance(result, dict):
        raise ValueError('Invalid model response')
    return result


def probe(path, expected=None):
    p = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                        'format=duration:stream=codec_type', '-of', 'json', str(path)],
                       capture_output=True, timeout=30, check=True)
    info = json.loads(p.stdout)
    duration = float(info['format']['duration'])
    if not math.isfinite(duration) or duration <= 0 or not any(s['codec_type'] == 'video' for s in info['streams']):
        raise ValueError('Invalid media')
    if expected and abs(duration - expected) > max(3, expected * .05):
        raise ValueError('Incomplete recording')
    return duration


def frame(path, second, crop=None, *, max_side=1024):
    filters = []
    if crop:
        x, y, right, bottom = bounds(crop)
        w, h = right - x, bottom - y
        filters.append(f'crop=iw*{w}:ih*{h}:iw*{x}:ih*{y}')
    # Portrait tray crops were enlarged to 1280 px wide, overflowing Qwen's
    # context when comparing several images. Bound both axes; never enlarge crops.
    filters.append(f"scale=w='min({max_side},iw)':h='min({max_side},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
                   if crop else 'scale=1280:-2')
    p = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(second), '-i', str(path),
                        '-frames:v', '1', '-vf', ','.join(filters), '-q:v', '3',
                        '-f', 'image2pipe', '-vcodec', 'mjpeg', '-'],
                       capture_output=True, timeout=30, check=True)
    if not p.stdout or len(p.stdout) > 5_000_000:
        raise ValueError('Invalid frame')
    return ('image/jpeg', p.stdout)


def validate_presence(reply, count):
    frames = reply.get('frames')
    if not isinstance(frames, list) or len(frames) != count:
        raise ValueError('Missing observations')
    for item in frames:
        if not isinstance(item, dict) or type(item.get('cat_visible')) is not bool or type(item.get('uncertain')) is not bool:
            raise ValueError('Invalid observation')
        boxes = item.get('boxes')
        if not isinstance(boxes, list) or len(set(boxes)) != len(boxes) or any(type(b) is not int or b not in (1, 2) for b in boxes):
            raise ValueError('Invalid boxes')
        if boxes and not item['cat_visible']:
            raise ValueError('Contradictory presence')
    return frames


def observe_presence(path, second, regions):
    for attempt in range(2):
        try:
            return _observe_presence(path, second, regions)
        except ValueError:
            if attempt:
                raise


def _observe_presence(path, second, regions):
    # Assign tray IDs in code. Inferring them from small trays in a full camera
    # image caused alternating 1/2 predictions and fragmented a single visit.
    boxes = sorted(regions['boxes'], key=lambda b: b['id'])
    reply = structured([frame(path, second, b['points'], max_side=640) for b in boxes],
        'Dwa obrazy to OSOBNE wycinki dwóch kuwet w TEJ SAMEJ CHWILI. '
        'Oceń każdy obraz niezależnie, w kolejności obrazów. '
        'occupied=true tylko gdy TUŁÓW kota jest wewnątrz kuwety. '
        'Sama głowa, łapa lub ogon kota stojącego obok nie oznacza zajętej kuwety. '
        'Człowiek nie jest kotem. Przy zasłonięciu lub dwóch kotach w jednej kuwecie uncertain=true. '
        'Nie identyfikuj kota ani rodzaju odchodów. Zwróć WYŁĄCZNIE JSON, dokładnie dwa elementy: '
        '{"frames":[{"occupied":false,"uncertain":false,"tail_base_point":null},'
        '{"occupied":false,"uncertain":false,"tail_base_point":null}]}. '
        'Jeśli widzisz nasadę ogona, tail_base_point=[x,y], współrzędne 0–1 względem '
        'TEGO WYCINKA. To połączenie ogona z tułowiem, nie głowa ani koniec ogona. '
        'Przy braku kota lub niepewnej lokalizacji tail_base_point=null.')
    samples = reply.get('frames')
    if not isinstance(samples, list) or len(samples) != len(boxes):
        raise ValueError('Missing tray observations')
    observation = dict(cat_visible=False, boxes=[], uncertain=False, uncertain_boxes=[], rear=[])
    for box, sample in zip(boxes, samples):
        if not isinstance(sample, dict) or any(type(sample.get(k)) is not bool for k in ('occupied', 'uncertain')):
            raise ValueError('Invalid tray observation')
        if sample['uncertain']:
            observation['uncertain_boxes'].append(box['id'])
        if not sample['occupied']:
            continue
        observation['cat_visible'] = True
        observation['boxes'].append(box['id'])
        point = sample.get('tail_base_point')
        if (not sample['uncertain'] and isinstance(point, list) and len(point) == 2
                and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in point)):
            x0, y0, x1, y1 = bounds(box['points'])
            observation['rear'].append({'box_id': box['id'],
                'point': [x0 + point[0]*(x1-x0), y0 + point[1]*(y1-y0)]})
    return observation


def intervals(observations):
    """Require two entry samples; tolerate one missed sample within a visit."""
    visits = []
    for box in (1, 2):
        run = []
        for observation in observations:
            present = (box in observation['boxes'] and not observation['uncertain']
                       and box not in observation.get('uncertain_boxes', []))
            if run and (observation['t'] - run[-1] > 2 * STEP or (len(run) == 1 and not present)):
                if len(run) >= 2:
                    visits.append({'box_id': box, 'first': run[0], 'last': run[-1]})
                run = []
            if present:
                run.append(observation['t'])
        if len(run) >= 2:
            visits.append({'box_id': box, 'first': run[0], 'last': run[-1]})
    return sorted(visits, key=lambda v: (v['first'], v['box_id']))


def waste_result(reply):
    if not isinstance(reply.get('notes'), str) or len(reply['notes']) > 2000:
        raise ValueError('Missing evidence notes')
    for key in ('before_after_clear', 'urine', 'feces'):
        if type(reply.get(key)) is not bool:
            raise ValueError('Invalid waste result')
    for key in ('urine_region', 'feces_region'):
        r = reply.get(key)
        if r is not None and (type(r) is not int or r not in range(9)):
            raise ValueError('Invalid waste location')
    # Absence of visible evidence is not proof the cat did not urinate.
    urine = reply['before_after_clear'] and reply['urine']
    feces = reply['before_after_clear'] and reply['feces']
    return {'outcome': 'both' if urine and feces else 'urine' if urine else 'feces' if feces else 'uncertain',
            'region': reply.get('urine_region') if urine else None,
            'feces_region': reply.get('feces_region') if feces else None,
            'note': reply['notes']}


def analyze_recording(path, regions, stopped=lambda: False, progress=lambda message: None, *, identity_profiles=None, presence_engine=None):
    path = Path(path)
    duration = probe(path)
    if duration > 600:
        return {'status': 'needs_review', 'visits': [], 'summary': 'Film dłuższy niż 10 minut wymaga ręcznej oceny.'}
    if len(regions.get('boxes', [])) != 2:
        raise ValueError('Calibrate both trays')
    times = [float(t) for t in range(0, math.ceil(duration), STEP) if t < duration - .1]
    engine = presence_engine or os.getenv('PRESENCE_ENGINE', 'qwen')
    presence_stats = None
    if engine == 'opencv':
        from opencv_presence import observe_recording, METHOD
        try:
            observations, presence_stats = observe_recording(path, regions, times, observe_presence, stopped, progress)
        except RuntimeError as error:
            raise ProcessingError(str(error)) from None
        method = METHOD
    elif engine == 'qwen':
        method = 'separate_tray_crops_v1'
        observations = []
        for start, t in enumerate(times):
            if stopped():
                raise InterruptedError()
            progress(f'Analiza obecności: {min(start + 1, len(times))}/{len(times)} klatek')
            observations.append(dict(observe_presence(path, t, regions), t=t))
    else:
        raise ProcessingError('Nieznany PRESENCE_ENGINE. Wybierz qwen lub opencv.')
    visits = intervals(observations)
    for index, visit in enumerate(visits):
        if stopped():
            raise InterruptedError()
        progress('Porównywanie żwirku przed i po obserwowanej wizycie')
        first, last = visit['first'], visit['last']
        before, after = max(0, first - STEP), min(duration - .15, last + STEP)
        sample_times = sorted(set([before, first, (first + last) / 2, last, after]))
        crop = next(b['points'] for b in regions['boxes'] if b['id'] == visit['box_id'])
        reply = structured([frame(path, t, crop) for t in sample_times],
            'To wycinki JEDNEJ kuwety, chronologicznie. Sekundy: ' + json.dumps(sample_times) +
            '. Zwróć tylko JSON: {"before_after_clear":false,"urine":false,"feces":false,'
            '"urine_region":null,"feces_region":null,"notes":"opis dowodów i ograniczeń po polsku"}. '
            'before_after_clear=true tylko gdy widać ten sam obszar żwirku bez kota przed wizytą i po niej. '
            'urine=true tylko przy nowej widocznej mokrej plamie z porównania, feces=true przy nowych '
            'widocznych odchodach. Cień, kopanie, kucanie, stare zabrudzenia nie wystarczą. '
            'Jeśli brak materiału przed/po lub kot zasłania, ustaw before_after_clear=false. '
            'region to siatka 3x3 na wycinku: 0,1,2 górny rząd obrazu; 3,4,5 środek; 6,7,8 dół. '
            'Nie ustalaj tożsamości ani diagnozy.')
        visit.update(waste_result(reply))
        focus = inspect_focus(path, observations, visit, crop, duration, index, frame, structured,
                              waste_result, stopped, progress)
        visit['focus'] = focus
        kinds = set()
        if visit['outcome'] in ('urine', 'both'):
            kinds.add('urine')
        if visit['outcome'] in ('feces', 'both'):
            kinds.add('feces')
        for evidence in focus['evidence']:
            kinds.add(evidence['kind'])
            visit['region' if evidence['kind'] == 'urine' else 'feces_region'] = evidence['region']
        visit['outcome'] = 'both' if len(kinds) == 2 else next(iter(kinds), 'uncertain')
        if focus['windows']:
            visit['note'] += (f" Analiza stałego obszaru: {focus['checked_frames']} wybranych klatek "
                              f"z {focus['decoded_frames']} próbek co 0,2 s. "
                              + ('Osiągnięto limit 3 fragmentów; materiał wymaga sprawdzenia. ' if focus['limited'] else '')
                              + ' '.join(e['note'] for e in focus['evidence']))
        else:
            visit['note'] += ' Nie ustalono stabilnego obszaru pod zadem; brak dokładniejszej analizy tego miejsca.'
        if first == 0 or last + STEP + .15 >= duration:
            visit.update(outcome='uncertain', region=None, feces_region=None)
        identity = {'cat_id': 'unknown', 'reason': 'Brak wzorców kotów.'}
        if identity_profiles is not None:
            progress('Porównywanie sylwetki i ogona ze wzorcami Kalinki i Kefira')
            identity = identify_visit(path, visit, crop, identity_profiles, frame)
        visit['identity'] = identity
        visit['cat_id'] = identity['cat_id']
        identity_note = ('Kot nierozpoznany. ' if visit['cat_id'] == 'unknown' else
                         f"Prawdopodobnie {visit['cat_id'].capitalize()} — porównanie ze wzorcami. ")
        visit['note'] = ('Automatyczna analiza próbna. ' + identity_note + 'Próbkowanie co 2 s; '
                         'czas oznacza pierwszą i ostatnią widoczną obecność, nie dokładne wejście/wyjście. '
                         'Godziny pochodzą z metadanych kamery; nadruk czasu na filmie nie jest odczytywany. '
                         + ('Wizyta może wykraczać poza nagranie. ' if first == 0 or last + STEP + .15 >= duration else '')
                         + visit['note'])
    status = 'analyzed' if visits else 'needs_review' if any(o['cat_visible'] or o['uncertain'] or o.get('uncertain_boxes') for o in observations) else 'no_cat_observed'
    sampling_note = (f"OpenCV + Qwen: sprawdzono modelem {presence_stats['qwen_presence_frames']}/{len(times)} klatek obecności. "
                     if presence_stats else '')
    return {'status': status, 'visits': visits, 'observations': observations,
            'presence_method': method, 'presence_stats': presence_stats, 'duration_seconds': duration,
            'sample_interval_seconds': STEP, 'model': __import__('os').getenv('QWEN_MODEL', ''),
            'regions': regions, 'summary': sampling_note + f'Przeanalizowano {len(times)} klatek; fragmenty obecności: {len(visits)}. '
            'Krótkie zdarzenia między klatkami mogły zostać pominięte.'}


def join_visit_segments(visits):
    """Present linked recording segments as one visit, including in CSV/stats."""
    combined, owners = [], {}
    for visit in sorted(visits, key=lambda v: v['entered_at']):
        parent = owners.get(visit.get('continuation_of'))
        # A separately reviewed segment remains visible with its own assessment.
        if parent is None or visit.get('reviewed_at'):
            current = dict(visit)
            owners[visit['id']] = current
            combined.append(current)
            continue
        parent.setdefault('segments', [dict(parent)])
        parent['segments'].append(dict(visit))
        parent['exited_at'] = max(parent['exited_at'], visit['exited_at'])
        if not parent.get('reviewed_at'):
            kinds = set()
            for row in (parent, visit):
                kinds.update({'both': {'urine', 'feces'}, 'urine': {'urine'},
                              'feces': {'feces'}}.get(row['outcome'], set()))
            parent['outcome'] = 'both' if len(kinds) == 2 else next(iter(kinds), 'uncertain')
            for kind, field in (('urine', 'region'), ('feces', 'feces_region')):
                if visit['outcome'] in (kind, 'both') and visit.get(field) is not None:
                    parent[field] = visit[field]
            parent['original_outcome'] = parent['outcome']
        owners[visit['id']] = parent
    # A recording is one toilet session for a recognised cat. Tray changes are
    # observations within that session, including separately reviewed observations.
    groups, sessions = {}, []
    for visit in combined:
        key = (visit['id'].split('-')[0], visit['cat_id'])
        eligible = visit.get('source') == 'camera' and visit['cat_id'] in ('kefir', 'kalinka')
        if not eligible or key not in groups:
            sessions.append(visit)
            if eligible:
                groups[key] = visit
            continue
        parent = groups[key]
        parts = parent.get('segments', [dict(parent)]) + visit.get('segments', [dict(visit)])
        parts.sort(key=lambda p: p['entered_at'])
        parent['segments'] = parts
        parent['box_ids'] = list(dict.fromkeys(p.get('box_id', parent['box_id']) for p in parts))
        parent['entered_at'] = min(parent['entered_at'], visit['entered_at'])
        parent['exited_at'] = max(parent['exited_at'], visit['exited_at'])
        # Keep evidence and location attached to its original tray. The newest
        # manual review is the session assessment; all other reviews stay in parts.
        if visit.get('reviewed_at') and visit['reviewed_at'] > (parent.get('reviewed_at') or ''):
            for field in ('id', 'outcome', 'reviewed_at', 'note', 'box_id', 'region',
                          'feces_region', 'urine_point', 'feces_point'):
                parent[field] = visit.get(field)
        elif not parent.get('reviewed_at'):
            outcomes = {p['outcome'] for p in parts if 'outcome' in p}
            urine, feces = bool(outcomes & {'urine', 'both'}), bool(outcomes & {'feces', 'both'})
            parent['outcome'] = 'both' if urine and feces else 'urine' if urine else 'feces' if feces else 'uncertain'
            # A session covering different trays has no single spatial marker.
            if len(parent['box_ids']) > 1:
                for field in ('region', 'feces_region', 'urine_point', 'feces_point'):
                    parent[field] = None
    return sorted(sessions, key=lambda v: v['entered_at'], reverse=True)


class AnalysisPipeline:
    def __init__(self, store, root):
        self.store, self.root = store, Path(root)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.download_process = None
        self.model_retry_at = 0

    def start(self):
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE camera_recordings SET status='pending_analysis', attempts=MAX(0,attempts-1) WHERE status IN ('downloading','analyzing')")
            self.model_retry_at = self.store.db.execute(
                "SELECT COALESCE(MAX(retry_at),0) FROM camera_recordings WHERE status='waiting_model'").fetchone()[0]
        self.thread.start()

    def close(self):
        self.stop.set()
        process = self.download_process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        self.thread.join(timeout=1)

    def update(self, row, **fields):
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE camera_recordings SET ' + ','.join(k + '=?' for k in fields) +
                                 ' WHERE camera_host=? AND start_epoch=?',
                                 (*fields.values(), row['camera_host'], row['start_epoch']))
        self.store.publish()

    def take(self):
        with self.store.lock, self.store.db:
            row = self.store.db.execute("SELECT * FROM camera_recordings WHERE status IN ('pending_analysis','retry','waiting_model') AND retry_at<=? ORDER BY start_epoch LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return None
            row = dict(row)
            self.store.db.execute("UPDATE camera_recordings SET status='downloading',attempts=attempts+1,error=NULL WHERE camera_host=? AND start_epoch=?", (row['camera_host'], row['start_epoch']))
        self.store.publish()
        return row

    def continuation(self, row, visit):
        """Link only matching cats visible across adjacent recording boundaries."""
        if visit['first'] != 0 or visit.get('cat_id') not in ('kalinka', 'kefir'):
            return None
        previous = self.store.db.execute('''SELECT * FROM camera_recordings
            WHERE camera_host=? AND end_epoch BETWEEN ? AND ? AND start_epoch<?
            AND status='analyzed' ORDER BY end_epoch DESC LIMIT 1''',
            (row['camera_host'], row['start_epoch'] - STEP, row['start_epoch'], row['start_epoch'])).fetchone()
        if not previous or not previous['analysis']:
            return None
        analysis = json.loads(previous['analysis'])
        # Old full-frame observations were too unreliable for boundary linking.
        if analysis.get('presence_method') not in ('separate_tray_crops_v1', 'opencv_verified_presence_v1'):
            return None
        duration = analysis.get('duration_seconds', previous['end_epoch'] - previous['start_epoch'])
        for index, part in enumerate(analysis.get('visits', [])):
            if (part['box_id'] == visit['box_id'] and part.get('cat_id') == visit['cat_id']
                    and part['last'] >= duration - STEP - .15):
                candidate = f"{previous['media_key']}-{index}"
                saved = self.store.db.execute('SELECT cat_id,box_id FROM visits WHERE id=?', (candidate,)).fetchone()
                if saved and saved['cat_id'] == visit['cat_id'] and saved['box_id'] == visit['box_id']:
                    return candidate
        return None

    def complete(self, row, result, key):
        with self.store.lock, self.store.db:
            current = self.store.db.execute('SELECT status,end_epoch FROM camera_recordings WHERE camera_host=? AND start_epoch=?', (row['camera_host'], row['start_epoch'])).fetchone()
            if current['status'] in ('analyzed', 'no_cat_observed', 'ignored_test'):
                return
            if current['end_epoch'] != row['end_epoch']:
                self.store.db.execute("UPDATE camera_recordings SET status='pending_analysis',attempts=0 WHERE camera_host=? AND start_epoch=?", (row['camera_host'], row['start_epoch']))
                return
            for i, visit in enumerate(result['visits']):
                visit_id = f'{key}-{i}'
                continuation = self.continuation(row, visit)
                start, end = iso(row['start_epoch'] + visit['first']), iso(row['start_epoch'] + visit['last'])
                inserted = self.store.db.execute('''INSERT OR IGNORE INTO visits
                    (id,cat_id,box_id,entered_at,exited_at,status,outcome,original_outcome,scenario,source,region,feces_region,note,continuation_of)
                    VALUES (?,?,?,?,?,'done',?,?,'camera','camera',?,?,?,?)''',
                    (visit_id, visit.get('cat_id') if visit.get('cat_id') in ('kalinka', 'kefir') else 'unknown', visit['box_id'], start, end, visit['outcome'], visit['outcome'],
                     visit['region'], visit['feces_region'], visit['note'], continuation)).rowcount
                if inserted and not continuation:
                    cat = visit.get('cat_id', 'unknown')
                    label = f'Prawdopodobnie {cat.capitalize()}' if cat in ('kalinka', 'kefir') else 'Kot nierozpoznany'
                    self.store.notice(visit_id, f"Wykryto prawdopodobną wizytę w kuwecie {visit['box_id']}. {label}; wynik wymaga sprawdzenia w historii.")
            saved_result = dict(result, completed_at=iso(time.time()))
            self.store.db.execute('''UPDATE camera_recordings SET status=?,analysis=?,media_key=?,error=NULL
                WHERE camera_host=? AND start_epoch=?''', (result['status'], json.dumps(saved_result, ensure_ascii=False), key, row['camera_host'], row['start_epoch']))
            if not result['visits']:
                self.store.notice(None, 'Analiza nagrania: ' + ('w próbkowanych klatkach nie wykryto kota.' if result['status'] == 'no_cat_observed' else 'materiał wymaga ręcznej oceny.'))
        self.store.publish()

    def process(self, row):
        key = hashlib.sha256(f"{row['camera_host']}:{row['start_epoch']}:{row['end_epoch']}".encode()).hexdigest()[:24]
        folder = recordings_path(self.root) / key
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'recording.mp4'
        expected = row['end_epoch'] - row['start_epoch']
        if not path.exists():
            partial = folder / 'download.mp4'
            partial.unlink(missing_ok=True)
            job = dict(row, env=str(self.root / '.env'), target=str(partial))
            process = subprocess.Popen([str(self.root / '.venv-tapo/bin/python'), str(self.root / 'recording_download.py')],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.download_process = process
            try:
                output, _ = process.communicate(json.dumps(job), timeout=440)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise ProcessingError(download_error({'error_code': 'timeout'})) from None
            except BaseException:
                process.kill()
                process.communicate()
                raise
            finally:
                self.download_process = None
            if self.stop.is_set():
                raise InterruptedError()
            try:
                reply = json.loads(output)
                if not isinstance(reply, dict):
                    reply = {}
            except (ValueError, TypeError):
                reply = {}
            if reply.get('error_code') == 'recording_missing':
                partial.unlink(missing_ok=True)
                self.update(row, status='missing_on_camera', media_key=None,
                            error='Nagranie nie istnieje już na karcie lub zmienił się jego zakres. Pobieranie i analiza pominięte.')
                return
            if process.returncode or not reply.get('ok'):
                raise ProcessingError(download_error(reply))
            try:
                probe(partial, expected)
            except Exception:
                raise ProcessingError('Pobrany film jest niekompletny lub nieczytelny. Qwen nie analizował filmu.') from None
            partial.replace(path)
        try:
            probe(path, expected)
        except Exception:
            raise ProcessingError('Lokalny film jest niekompletny lub nieczytelny. Qwen nie analizował filmu.') from None
        # Keep downloading new recordings during an outage, without calling the
        # unavailable model for each film or postponing the shared recovery probe.
        if time.time() < self.model_retry_at:
            self.update(row, media_key=key)
            raise ModelUnavailable()
        self.update(row, status='analyzing', media_key=key)
        try:
            result = analyze_recording(path, self.store.camera_regions(), self.stop.is_set,
                                      lambda message: self.update(row, error=message),
                                      identity_profiles=self.root / 'data' / 'cat_profiles' / 'manifest.json')
        except (ModelUnavailable, InterruptedError, ProcessingError):
            raise
        except HTTPError as error:
            raise ProcessingError(f'Film pobrany i zachowany. Qwen odrzucił zapytanie analizy (HTTP {error.code}).') from None
        except Exception:
            raise ProcessingError('Film pobrany i zachowany. Błąd analizy: odczytu klatek lub odpowiedzi Qwena.') from None
        if not self.stop.is_set():
            self.model_retry_at = 0
            self.complete(row, result, key)

    def wait_for_model(self, row):
        if self.model_retry_at <= time.time():
            self.model_retry_at = time.time() + 60
        self.update(row, status='waiting_model', attempts=row['attempts'], retry_at=self.model_retry_at,
                    error='Qwen niedostępny. Film zachowany; analiza wznowi się automatycznie. '
                          'Ponawianie połączenia co minutę, bez limitu prób.')

    def run(self):
        while not self.stop.is_set():
            row = self.take()
            if not row:
                self.stop.wait(5)
                continue
            try:
                self.process(row)
            except InterruptedError:
                return
            except ModelUnavailable:
                self.wait_for_model(row)
            except Exception as error:
                # Raw library/model/network errors may contain credentials or private URLs.
                exhausted = row['attempts'] + 1 >= 3
                self.update(row, status='failed' if exhausted else 'retry', retry_at=time.time() + 300,
                            error=(str(error) if isinstance(error, ProcessingError) else 'Błąd przetwarzania nagrania.') + ' ' +
                            ('Wymagana ręczna kontrola po 3 próbach.' if exhausted else 'Ponowienie za 5 minut.'))
