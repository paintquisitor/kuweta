"""Conservative visual sampling: OpenCV measures change; Qwen identifies cats.

No background is assumed empty, and motion alone never creates an observation.
Only matching model observations bounding a visually stable interval may be reused.
"""
import copy
import json
import subprocess
import time

METHOD = 'opencv_verified_presence_v1'
MAX_GAP = 10  # Seconds between model checks, including a motionless cat.
CHANGE_LIMIT = .04  # Fraction of a tray changing significantly from an anchor.


def dependencies():
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise RuntimeError('Tryb OpenCV wymaga requirements-opencv.txt w środowisku serwera.') from None
    return cv2, np


def changed_fraction(before, after, mask):
    cv2, np = dependencies()
    # Small exposure changes are common in IR. Large light changes must still
    # trigger a model check, so compensation is deliberately bounded.
    delta = after.astype(np.int16) - before.astype(np.int16)
    exposure = float(np.clip(np.median(delta[mask > 0]), -8, 8))
    changed = ((np.abs(delta - exposure) > 20) & (mask > 0)).astype(np.uint8)
    changed = cv2.morphologyEx(changed, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return float(np.count_nonzero(changed & (mask > 0)) / np.count_nonzero(mask))


def scan_motion(path, regions, times, stopped=lambda: False, progress=lambda message: None):
    """Read local video, measure only calibrated polygons, keep no full frames."""
    cv2, np = dependencies()
    started = time.monotonic()
    # OpenCV seeks relative to the video stream, while FFmpeg (-ss) and the
    # recording timeline use the container start. Tapo audio can start ~2 s early.
    info = json.loads(subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
         'stream=start_time:format=start_time', '-of', 'json', str(path)],
        capture_output=True, check=True, timeout=30).stdout)
    video_offset = float(info['streams'][0].get('start_time', 0)) - float(info.get('format', {}).get('start_time', 0))
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        raise ValueError('OpenCV nie może odczytać filmu.')
    masks, crops, anchor, previous = [], [], None, None
    rows, selected, last_anchor = [], set(), 0
    try:
        for index, second in enumerate(times):
            if stopped():
                raise InterruptedError()
            progress(f'OpenCV: porównywanie kuwet {index + 1}/{len(times)}')
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0, second - video_offset) * 1000)
            ok, image = cap.read()
            if not ok:
                raise ValueError('OpenCV: niepełny odczyt filmu.')
            height, width = image.shape[:2]
            scale = min(1, 1280 / width)
            image = cv2.resize(image, (round(width * scale), round(height * scale)))
            gray = cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (5, 5), 0)
            height, width = gray.shape
            if not masks:
                for box in sorted(regions['boxes'], key=lambda b: b['id']):
                    polygon = np.array([[round(x * (width-1)), round(y * (height-1))]
                                        for x, y in box['points']], np.int32)
                    x, y, w, h = cv2.boundingRect(polygon)
                    mask = np.zeros((h, w), np.uint8)
                    cv2.fillPoly(mask, [polygon - [x, y]], 1)
                    if not np.count_nonzero(mask):
                        raise ValueError('Pusty obszar kuwety.')
                    masks.append(mask)
                    crops.append((x, y, w, h))
            current = [gray[y:y+h, x:x+w].copy() for x, y, w, h in crops]
            differences = [changed_fraction(a, b, m) for a, b, m in zip(anchor, current, masks)] if anchor else [0.] * len(masks)
            motion = [changed_fraction(a, b, m) for a, b, m in zip(previous, current, masks)] if previous else [0.] * len(masks)
            keyframe = (index == 0 or max(differences) >= CHANGE_LIMIT
                        or second - times[last_anchor] >= MAX_GAP or index == len(times)-1)
            rows.append({'t': second, 'motion': motion, 'anchor_difference': differences,
                         'keyframe': keyframe})
            if keyframe:
                selected.add(index)
                # Inspect the last frame before a visual transition as well.
                if index and max(differences) >= CHANGE_LIMIT:
                    selected.add(index-1)
                anchor, last_anchor = current, index
            previous = current
    finally:
        cap.release()
    return {'samples': rows, 'selected': sorted(selected),
            'scan_seconds': round(time.monotonic() - started, 3)}


def same_presence(a, b):
    # Uncertainty is never propagated as a positive observation.
    return (not a.get('uncertain') and not b.get('uncertain')
            and not a.get('uncertain_boxes') and not b.get('uncertain_boxes')
            and a['cat_visible'] == b['cat_visible'] and sorted(a['boxes']) == sorted(b['boxes']))


def observe_recording(path, regions, times, observe, stopped=lambda: False, progress=lambda message: None):
    scan = scan_motion(path, regions, times, stopped, progress)
    observations, calls = {}, 0

    def check(index):
        nonlocal calls
        if index not in observations:
            if stopped():
                raise InterruptedError()
            progress(f'OpenCV + Qwen: sprawdzanie klatki {index+1}/{len(times)} (wywołanie {calls+1})')
            observations[index] = dict(observe(path, times[index], regions), t=times[index],
                                       presence_source='qwen')
            calls += 1
        return observations[index]

    selected = scan['selected']
    for index in selected:
        check(index)
    for left, right in zip(selected, selected[1:]):
        agree = same_presence(observations[left], observations[right])
        for index in range(left+1, right):
            if stopped():
                raise InterruptedError()
            if agree:
                # Both ends were independently checked. Never copy tail points:
                # landmark coordinates require an actual model observation.
                observation = copy.deepcopy(observations[left])
                observation.update(t=times[index], rear=[], presence_source='opencv_bridge',
                                   verified_between=[times[left], times[right]])
                observations[index] = observation
            else:
                check(index)  # Resolve changes/occlusion at the original 2 s resolution.
    stats = {'method': METHOD, 'sampled_frames': len(times), 'qwen_presence_frames': calls,
             'reused_presence_frames': len(times)-calls, 'max_check_gap_seconds': MAX_GAP,
             'change_limit': CHANGE_LIMIT, 'scan_seconds': scan['scan_seconds'],
             'motion': scan['samples']}
    return [observations[i] for i in range(len(times))], stats
