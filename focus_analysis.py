"""Inspect a fixed litter area after a stationary cat changes position."""
import math
import subprocess

FINE_STEP = .2
MAX_WINDOWS = 3

def bounds(points, margin=.015):
    xs, ys = zip(*points)
    return (max(0, min(xs) - margin), max(0, min(ys) - margin),
            min(1, max(xs) + margin), min(1, max(ys) + margin))


def rear_point(observation, box, polygon):
    if observation.get('uncertain') or box in observation.get('uncertain_boxes', []) or box not in observation['boxes']:
        return None
    rear = observation.get('rear', [])
    if not isinstance(rear, list):
        return None
    matches = [r for r in rear if isinstance(r, dict) and r.get('box_id') == box]
    if len(matches) != 1:
        return None
    point = matches[0].get('point')
    if not isinstance(point, list) or len(point) != 2 or any(
            type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in point):
        return None
    # Reject points outside the calibrated tray, including its bounding-box corners.
    signs = []
    for a, b in zip(polygon, polygon[1:] + polygon[:1]):
        signs.append((b[0]-a[0])*(point[1]-a[1]) - (b[1]-a[1])*(point[0]-a[0]))
    if not (all(s >= -1e-9 for s in signs) or all(s <= 1e-9 for s in signs)):
        return None
    return point


def tail_in_tray(point, polygon):
    """Map the cropped-image landmark back to the calibrated tray polygon."""
    if not isinstance(point, list) or len(point) != 2 or any(
            type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 1 for n in point):
        return False
    x0, y0, x1, y1 = bounds(polygon)
    global_point = [x0 + point[0] * (x1-x0), y0 + point[1] * (y1-y0)]
    return rear_point({'boxes': [1], 'rear': [{'box_id': 1, 'point': global_point}]},
                      1, polygon) is not None


def focus_windows(observations, visit, polygon, duration):
    x0, y0, x1, y1 = bounds(polygon)
    width, height = x1-x0, y1-y0
    stable, windows = [], []
    for o in observations:
        if not visit['first'] <= o['t'] <= visit['last'] + 2:
            continue
        point = rear_point(o, visit['box_id'], polygon)
        anchor = stable[0]['point'] if stable else None
        moved = anchor and (point is None or math.hypot(
            (point[0]-anchor[0])/width, (point[1]-anchor[1])/height) > .08)
        if moved:
            if len(stable) >= 2:
                center = [sum(s['point'][i] for s in stable)/len(stable) for i in (0, 1)]
                # Freeze this area; later cat positions never move it.
                left, top = max(x0, center[0]-.18*width), max(y0, center[1]-.18*height)
                right, bottom = min(x1, center[0]+.18*width), min(y1, center[1]+.18*height)
                windows.append({'start': stable[-1]['t'], 'end': min(duration-.15, visit['last']+2, o['t']+2),
                                'stationary_since': stable[0]['t'], 'point': center,
                                'crop': [[left, top], [right, top], [right, bottom], [left, bottom]]})
            stable = []
        if point is not None:
            stable.append({'point': point, 't': o['t']})
    return windows


def verify_rear_windows(path, windows, polygon, frame, structured, stopped, progress, *, index=0):
    """Use repeated tail-base locations, never agreement with the old candidate."""
    x0, y0, x1, y1 = bounds(polygon)
    accepted, checks = [], []
    for window in windows:
        if stopped():
            raise InterruptedError()
        t = round((window['stationary_since'] + window['start']) / 2, 3)
        progress('Lokalizowanie nasady ogona na wycinku kuwety')
        def locate(second):
            if stopped():
                raise InterruptedError()
            crop_image = frame(path, second, polygon, max_side=1024)
            try:
                context = (
                    'Locate ONLY the TAIL BASE: the junction where the tail attaches to the rump. '
                    'Follow the visible tail toward the torso and mark that junction, not the tail tip '
                    'or the middle of the back. The head may be outside the image; head visibility '
                    'is irrelevant and must not reduce confidence in a visible tail base. '
                    'Use only visible image evidence; do not infer position from tray orientation. '
                    'Coordinates: x from left to right, y from top to bottom, both normalized 0 to 1 '
                    'relative to this image. Return one compact JSON object, no other text. Fields: '
                    'tail_base_point: [x,y] or null if unlocalizable; '
                    'tail_base_clear: true only if the tail-to-rump junction is clearly visible, '
                    'otherwise false (an uncertain estimate is allowed); '
                    'explanation: one short sentence in Polish, at most 120 characters. '
                    'Do not locate or describe the head. Do not classify urine or feces.')
                reply = structured([crop_image], context, task='anatomy')
            except ValueError:
                reply = {'tail_base_clear': False, 'invalid_response': True,
                         'explanation': 'Qwen nie zwrócił poprawnych współrzędnych w formacie JSON.'}
            tail = reply.get('tail_base_point')
            if not (isinstance(tail, list) and len(tail) == 2 and all(
                    type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in tail)):
                tail = None
            if tail is not None and not tail_in_tray(tail, polygon):
                reply['rejected_tail_point'] = tail
                tail = None
            return crop_image, reply, tail

        crop_image, reply, tail = locate(t)
        point = [(window['point'][0]-x0)/(x1-x0), (window['point'][1]-y0)/(y1-y0)]
        samples = [{'t': t, 'point': tail, 'clear': reply.get('tail_base_clear') is True}]
        if tail is not None:
            for second in (window['stationary_since'], window['start']):
                _, other_reply, other_tail = locate(second)
                samples.append({'t': second, 'point': other_tail,
                                'clear': other_reply.get('tail_base_clear') is True})
        consistent = (len(samples) == 3 and all(tail_in_tray(s['point'], polygon) for s in samples)
                      and all(math.dist(a['point'], b['point']) <= .08
                              for a in samples for b in samples))
        name = f'visit-{index}-anatomy-{len(checks)}.jpg'
        (path.parent / name).write_bytes(crop_image[1])
        checks.append({'t': t, 'file': name, 'candidate_point': point,
                       'accepted': bool(consistent), 'tail_base_point': tail,
                       'rejected_tail_point': reply.get('rejected_tail_point'),
                       'target': 'tail_base', 'verification': 'temporal_tail_base', 'samples': samples,
                       'clear': reply.get('tail_base_clear') is True,
                       'explanation': str(reply.get('explanation', ''))[:1000],
                       'reason': 'outside_tray' if reply.get('rejected_tail_point') is not None else 'invalid_response' if reply.get('invalid_response') else 'tail_consistent' if consistent else 'tail_unstable' if tail is not None else 'anatomy_unclear'})
        if consistent:
            # Keep the midpoint location aligned with its displayed photograph.
            # Self-reported clarity is retained, but is not a veto on repeated estimates.
            center = [x0+tail[0]*(x1-x0), y0+tail[1]*(y1-y0)]
            left, top = max(x0, center[0]-.18*(x1-x0)), max(y0, center[1]-.18*(y1-y0))
            right, bottom = min(x1, center[0]+.18*(x1-x0)), min(y1, center[1]+.18*(y1-y0))
            accepted.append(dict(window, point=center, source='tail_base',
                                 crop=[[left, top], [right, top], [right, bottom], [left, bottom]]))
    return accepted, checks


def motion_samples(path, window, stopped):
    """Decode at 5 fps; retain uniform samples plus the strongest local changes."""
    x0, y0, x1, y1 = bounds(window['crop'])
    previous, scores = None, []
    count = int((window['end']-window['start'])/FINE_STEP + 1e-6)+1
    for index in range(count):
        if stopped():
            raise InterruptedError()
        t = round(window['start']+index*FINE_STEP, 3)
        result = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(t), '-i', str(path),
            '-frames:v', '1', '-vf', f'crop=iw*{x1-x0}:ih*{y1-y0}:iw*{x0}:ih*{y0},scale=64:64,format=gray',
            '-f', 'rawvideo', '-'], capture_output=True, check=True, timeout=30)
        pixels = result.stdout
        if len(pixels) != 4096:
            raise ValueError('Invalid motion frame')
        score = sum(abs(a-b) for a, b in zip(pixels, previous))/4096 if previous else 0
        scores.append((t, score))
        previous = pixels
    selected = {0, len(scores)-1, len(scores)//3, 2*len(scores)//3}
    for i in sorted(range(len(scores)), key=lambda i: scores[i][1], reverse=True):
        if len(selected) >= min(8, len(scores)):
            break
        selected.add(i)
    return [scores[i][0] for i in sorted(selected)], len(scores)


def tray_region(region, crop, polygon):
    if region is None:
        return None
    x0, y0, x1, y1 = bounds(crop)
    bx0, by0, bx1, by1 = bounds(polygon)
    x = x0 + (region % 3 + .5)/3*(x1-x0)
    y = y0 + (region // 3 + .5)/3*(y1-y0)
    return min(2, max(0, int((y-by0)/(by1-by0)*3)))*3 + min(2, max(0, int((x-bx0)/(bx1-bx0)*3)))


def posture_suggestions(path, windows, polygon, index, frame):
    """Save review aids separately from visible waste evidence and visit outcomes."""
    x0, y0, x1, y1 = bounds(polygon)
    suggestions, seen = [], set()
    # Prefer longer stationary observations; two samples alone are too brief.
    for window in sorted(windows, key=lambda w: w['start']-w['stationary_since'], reverse=True):
        stationary = window['start'] - window['stationary_since']
        if stationary < 4:
            continue
        x, y = ((window['point'][0]-x0)/(x1-x0), (window['point'][1]-y0)/(y1-y0))
        region = min(2, max(0, int(y*3)))*3 + min(2, max(0, int(x*3)))
        if region in seen:
            continue
        seen.add(region)
        t = round((window['stationary_since']+window['start'])/2, 3)
        name = f'visit-{index}-posture-{len(suggestions)}.jpg'
        (path.parent / name).write_bytes(frame(path, t, polygon, max_side=640)[1])
        suggestions.append({'file': name, 't': t, 'region': region, 'point': [x, y],
                            'stationary_seconds': stationary, 'source': 'posture', 'kind': 'unknown',
                            'location_source': window.get('source', 'legacy_candidate')})
        if len(suggestions) == MAX_WINDOWS:
            break
    return suggestions


def inspect_focus(path, observations, visit, polygon, duration, index, frame, structured,
                  waste_result, stopped, progress):
    windows = focus_windows(observations, visit, polygon, duration)
    verified, checks = verify_rear_windows(path, windows[:MAX_WINDOWS], polygon, frame,
                                          structured, stopped, progress, index=index)
    result = {'windows': verified, 'pose_checks': checks, 'evidence': [], 'decoded_frames': 0,
              'checked_frames': 0, 'limited': len(windows) > MAX_WINDOWS}
    result['suggestions'] = posture_suggestions(path, verified, polygon, index, frame)
    if visit['first'] == 0:
        return result  # No pre-visit reference in this recording.
    for wi, window in enumerate(result['windows']):
        if stopped():
            raise InterruptedError()
        progress('Dokładna analiza odsłaniania żwirku: wybór klatek przy 5 kl./s')
        times, count = motion_samples(path, window, stopped)
        result['decoded_frames'] += count
        baseline = frame(path, max(0, visit['first']-2), window['crop'])
        for t in times:
            if stopped():
                raise InterruptedError()
            progress(f'Sprawdzanie stałego obszaru pod zadem: {t:.1f} s filmu')
            current = frame(path, t, window['crop'])
            reply = structured([baseline, current],
                'Dwa wycinki DOKŁADNIE TEGO SAMEGO stałego miejsca w kuwecie. Obraz 1 przed wizytą, '
                f'obraz 2 w {t:.1f} s filmu, podczas lub po przesunięciu kota. '
                'Szukaj nowego widocznego śladu, także chwilowo odsłoniętego przed zakopaniem. '
                'Zwróć JSON: {"before_after_clear":false,"urine":false,"feces":false,'
                '"urine_region":null,"feces_region":null,"notes":"krótki opis dowodu"}. '
                'before_after_clear=true wyłącznie gdy miejsce śladu jest odsłonięte na OBU obrazach '
                'i można je porównać. urine=true wymaga nowej widocznej mokrej plamy, feces=true nowych '
                'widocznych odchodów. Cień, zmiana oświetlenia, rozkopany żwirek, postawa i zakopywanie '
                'NIE potwierdzają wydalenia. Przy zasłonięciu lub wątpliwości ustaw false. '
                'Lokalizacja to siatka 3x3 TEGO WYCINKA: 0,1,2 góra, 3,4,5 środek, 6,7,8 dół.')
            waste = waste_result(reply)
            result['checked_frames'] += 1
            for kind, field in (('urine', 'region'), ('feces', 'feces_region')):
                if waste['outcome'] not in (kind, 'both') or any(e['kind'] == kind for e in result['evidence']):
                    continue
                name = f'visit-{index}-{kind}.jpg'
                reference = f'visit-{index}-{kind}-before.jpg'
                # Write only generated names next to the downloaded recording.
                (path.parent / name).write_bytes(current[1])
                (path.parent / reference).write_bytes(baseline[1])
                result['evidence'].append({'kind': kind, 'file': name, 'before_file': reference,
                    't': t, 'before_t': max(0, visit['first']-2), 'crop': window['crop'],
                    'window': wi, 'region': tray_region(waste[field], window['crop'], polygon),
                    'note': waste['note']})
    return result
