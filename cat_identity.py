"""Conservative visual reference matching; no model training or self-labelling."""
import json
from pathlib import Path

from qwen import analyze_images

CAT_IDS = {'kalinka', 'kefir'}
METHOD = 'qwen_visual_references_v1'


def identity_crop(polygon):
    """Keep the tray as a size reference, with space for a projecting tail."""
    xs, ys = zip(*polygon)
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    left, right = max(0, min(xs) - width * .35), min(1, max(xs) + width * .35)
    top, bottom = max(0, min(ys) - height * .15), min(1, max(ys) + height * .5)
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def load_profiles(manifest):
    manifest = Path(manifest)
    data = json.loads(manifest.read_text())
    profiles = data['profiles']
    if data['version'] != 1 or len(profiles) != 2 or {p['cat_id'] for p in profiles} != CAT_IDS:
        raise ValueError('Invalid cat profiles')
    images, labels = [], []
    for profile in profiles:
        if profile['label_source'] != 'user_confirmed' or not 2 <= len(profile['references']) <= 3:
            raise ValueError('References must be user-confirmed')
        for reference in profile['references']:
            path = (manifest.parent / reference['file']).resolve()
            if path.parent != manifest.parent.resolve() or path.suffix != '.jpg':
                raise ValueError('Invalid reference path')
            image = path.read_bytes()
            if not image or len(image) > 5_000_000:
                raise ValueError('Invalid reference image')
            images.append(('image/jpeg', image))
            labels.append({'image': len(images), 'cat_id': profile['cat_id'],
                           'description': profile['description']})
    return data, images, labels


def decide(reply, count):
    """Two clear matching observations, no contradictory vote; otherwise unknown."""
    observations = reply.get('observations')
    if not isinstance(observations, list) or len(observations) != count:
        raise ValueError('Missing identity observations')
    votes = []
    for observation in observations:
        if not isinstance(observation, dict) or observation.get('cat_id') not in CAT_IDS | {'unknown'}:
            raise ValueError('Invalid cat identity')
        if (type(observation.get('cat_count')) is not int or observation['cat_count'] not in (0, 1, 2)
                or any(type(observation.get(k)) is not bool for k in ('body_clear', 'tail_clear', 'uncertain'))
                or not isinstance(observation.get('reason'), str) or len(observation['reason']) > 1000):
            raise ValueError('Invalid identity evidence')
        if observation['cat_count'] > 1:
            return 'unknown', 'W kadrze jest więcej niż jeden kot.'
        if observation['cat_id'] in CAT_IDS:
            votes.append(observation['cat_id'])
    clear = [o['cat_id'] for o in observations if o['cat_count'] == 1 and o['body_clear']
             and o['tail_clear'] and not o['uncertain'] and o['cat_id'] in CAT_IDS]
    if len(set(votes)) == 1 and len(clear) >= 2:
        return clear[0], f'Zgodne porównanie sylwetki i ogona w {len(clear)} ujęciach.'
    return 'unknown', 'Brak co najmniej dwóch zgodnych, wyraźnych ujęć sylwetki i ogona.'


def identify_visit(path, visit, polygon, manifest, frame_reader):
    result = {'cat_id': 'unknown', 'method': METHOD, 'source': 'automatic'}
    try:
        profiles, images, labels = load_profiles(manifest)
    except (OSError, ValueError, KeyError, TypeError):
        return dict(result, reason='Brak kompletnych, potwierdzonych wzorców kotów.')
    first, last = visit['first'], visit['last']
    if last - first < 4:
        return dict(result, reason='Za krótka obserwacja do porównania kilku ujęć kota.')
    times = [round(first + (last - first) * part, 2) for part in (.2, .5, .8)]
    crop = identity_crop(polygon)
    result.update(sample_times=times, reference_set=profiles['id'], reference_images=labels)
    context = (
        f'Pierwsze {len(images)} obrazów to podpisane WZORCE, nie klasyfikuj ich. Podpisy: ' + json.dumps(labels, ensure_ascii=False)
        + f'. TYLKO ostatni obraz (numer {len(images)+1}) przedstawia kota DO ROZPOZNANIA. '
        'Nie używaj czasu, tła, kuwety ani kolejności wzorców do zgadywania imienia. '
        'Porównuj proporcje ciała względem kuwety (z uwzględnieniem pozy), szerokość grzbietu '
        'oraz puszystość i kształt ogona. Same rozmiary lub sama pozycja nie wystarczają. '
        'Zgięcie tułowia i obrót zmieniają widoczny rozmiar; porównaj też wzorce w podobnej pozycji. '
        'Obraz IR nie pozwala wnioskować o kolorze sierści. Zmiana światła, zasłonięcie ogona, '
        'brak kota lub niewystarczające podobieństwo oznaczają cat_id="unknown", uncertain=true. '
        'Zwróć WYŁĄCZNIE zwięzły JSON: {"cat_id":"unknown",'
        '"cat_count":0,"body_clear":false,"tail_clear":false,"uncertain":true,'
        '"reason":"krótkie uzasadnienie po polsku"}. '
        'cat_id: kalinka/kefir/unknown. cat_count: 0,1 lub 2 (dwa lub więcej kotów).')
    image_labels = [f"WZORZEC {label['image']}: {label['cat_id']}. {label['description']}" for label in labels]
    image_labels.append('OBRAZ DO ROZPOZNANIA. Który kot jest na TYM obrazie?')
    # Connectivity errors propagate to the persistent recording retry queue.
    observations = []
    try:
        for second in times:
            target = frame_reader(path, second, crop, max_side=640)
            text = analyze_images(images + [target], context, json_output=True, task='identity',
                                  image_labels=image_labels).strip()
            if text.startswith('```'):
                text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
            observation = json.loads(text)
            decide({'observations': [observation]}, 1)  # Validate each response before continuing.
            observations.append(dict(observation, t=second))
        cat_id, reason = decide({'observations': observations}, len(times))
    except (ValueError, KeyError, TypeError):
        return dict(result, reason='Niepoprawna odpowiedź rozpoznawania; kot pozostaje nierozpoznany.')
    return dict(result, cat_id=cat_id, reason=reason, observations=observations)
