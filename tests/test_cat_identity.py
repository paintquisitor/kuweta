import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cat_identity import decide, identify_visit, identity_crop, load_profiles
from qwen import ModelUnavailable


def observation(cat='kalinka', **changes):
    return dict({'cat_id': cat, 'cat_count': 1, 'body_clear': True,
                 'tail_clear': True, 'uncertain': False, 'reason': 'Sylwetka i ogon pasują.'}, **changes)


class CatIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root / 'manifest.json'
        profiles = []
        for cat in ('kalinka', 'kefir'):
            refs = []
            for index in (1, 2):
                name = f'{cat}-{index}.jpg'
                (self.root / name).write_bytes(name.encode())
                refs.append({'file': name})
            profiles.append({'cat_id': cat, 'label_source': 'user_confirmed',
                             'description': cat, 'references': refs})
        self.manifest.write_text(json.dumps({'version': 1, 'id': 'test-set', 'profiles': profiles}))
        self.polygon = [[.4,.1],[.55,.1],[.55,.45],[.4,.45]]
        self.visit = {'first': 0, 'last': 40, 'box_id': 1}

    def identify(self, reader=None, **kwargs):
        return identify_visit('video.mp4', self.visit, self.polygon, self.manifest,
                              reader or Mock(return_value=('image/jpeg', b'target')), **kwargs)

    def test_consensus_requires_clear_distinct_views_without_conflicting_identity(self):
        cases = [
            ([observation(), observation(), observation('unknown', uncertain=True)], 'kalinka'),
            ([observation('kefir')] * 3, 'kefir'),
            ([observation(), observation(), observation('kefir')], 'unknown'),
            ([observation(), observation(tail_clear=False), observation(uncertain=True)], 'unknown'),
            ([observation('unknown', cat_count=0)] * 3, 'unknown'),
            ([observation(), observation(), observation(cat_count=2)], 'unknown'),
        ]
        for observations, expected in cases:
            with self.subTest(observations=observations):
                self.assertEqual(decide({'observations': observations}, 3)[0], expected)

    def test_reference_images_and_three_bounded_target_frames_reach_adapter(self):
        reader = Mock(return_value=('image/jpeg', b'target'))
        with patch('cat_identity.analyze_images', return_value=json.dumps(observation())) as model:
            result = self.identify(reader)
        self.assertEqual(result['cat_id'], 'kalinka')
        self.assertEqual(result['sample_times'], [8, 20, 32])
        self.assertEqual(result['reference_set'], 'test-set')
        sent = model.call_args.args[0]
        self.assertEqual(model.call_count, 3)
        self.assertEqual(len(sent), 5)
        self.assertEqual([data for mime, data in sent[:4]],
                         [f'{cat}-{n}.jpg'.encode() for cat in ('kalinka', 'kefir') for n in (1,2)])
        for call in reader.call_args_list:
            self.assertEqual(call.kwargs['max_side'], 640)
            self.assertEqual(call.args[2], identity_crop(self.polygon))
        self.assertEqual(model.call_args.kwargs['task'], 'identity')
        self.assertEqual(len(model.call_args.kwargs['image_labels']), 5)

    def test_unavailable_model_enters_retry_path_but_bad_response_stays_unknown(self):
        with patch('cat_identity.analyze_images', side_effect=ModelUnavailable()):
            with self.assertRaises(ModelUnavailable):
                self.identify()
        for response in ('not json', '[]', '{"observations":[]}',
                         json.dumps({'observations': [observation('other')] * 3})):
            with self.subTest(response=response), patch('cat_identity.analyze_images', return_value=response):
                self.assertEqual(self.identify()['cat_id'], 'unknown')

    def test_missing_or_unconfirmed_references_do_not_call_model(self):
        data = json.loads(self.manifest.read_text())
        data['profiles'][0]['label_source'] = 'automatic'
        self.manifest.write_text(json.dumps(data))
        with patch('cat_identity.analyze_images') as model:
            self.assertEqual(self.identify()['cat_id'], 'unknown')
            self.manifest.unlink()
            self.assertEqual(self.identify()['cat_id'], 'unknown')
            model.assert_not_called()

    def test_reference_file_cannot_escape_profile_directory(self):
        data = json.loads(self.manifest.read_text())
        data['profiles'][0]['references'][0]['file'] = '../outside.jpg'
        self.manifest.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            load_profiles(self.manifest)

    def test_short_visit_keeps_unknown_without_model_request(self):
        self.visit['last'] = 2
        with patch('cat_identity.analyze_images') as model:
            self.assertEqual(self.identify()['cat_id'], 'unknown')
            model.assert_not_called()
