import json
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from analysis_pipeline import AnalysisPipeline, ProcessingError, analyze_recording, frame, intervals, validate_presence, waste_result, observe_presence, join_visit_segments, structured
from server import Store
from focus_analysis import bounds, focus_windows, rear_point, tray_region, posture_suggestions, inspect_focus
from qwen import ModelUnavailable


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'test.sqlite3', seed=False)
        self.pipeline = AnalysisPipeline(self.store, self.temp.name)
        self.result = {'camera_host': '192.0.2.1', 'recordings': [{'startTime': 1790184058, 'endTime': 1790184124}],
                       'checked_through': 1790184258, 'clock_offset_seconds': 0}
        self.store.recordings.ingest(self.result, 1790184258)
        self.regions = {'boxes': [{'id': 1, 'points': [[0,0],[.4,0],[.4,1],[0,1]]},
                                  {'id': 2, 'points': [[.6,0],[1,0],[1,1],[.6,1]]}]}

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_structured_accepts_qwen_json_with_trailing_backtick(self):
        reply = {'frames': [{'occupied': False, 'uncertain': False}]}
        with patch('analysis_pipeline.analyze_images', return_value=json.dumps(reply) + '`'):
            self.assertEqual(structured([('image/jpeg', b'image')], 'context'), reply)

    def test_tray_crops_assign_ids_and_ignore_tail_coordinates(self):
        reply = {'frames': [dict(occupied=False, uncertain=True, tail_base_point=None),
                            dict(occupied=True, uncertain=False, tail_base_point=[.5, .25])]}
        with patch('analysis_pipeline.frame', return_value=('image/jpeg', b'crop')) as reader, \
             patch('analysis_pipeline.structured', return_value=reply):
            observation = observe_presence('clip.mp4', 12, self.regions)
        self.assertEqual(observation['boxes'], [2])
        self.assertEqual(observation['uncertain_boxes'], [1])
        self.assertEqual([c.args[2] for c in reader.call_args_list],
                         [b['points'] for b in self.regions['boxes']])
        point = rear_point(observation, 2, self.regions['boxes'][1]['points'])
        self.assertIsNone(point)
        with patch('analysis_pipeline.frame', return_value=('image/jpeg', b'crop')), \
             patch('analysis_pipeline.structured', side_effect=[ValueError('Invalid JSON'), reply]) as model:
            self.assertEqual(observe_presence('clip.mp4', 12, self.regions)['boxes'], [2])
            self.assertEqual(model.call_count, 2)
        for malformed in ({'frames': []}, {'frames': [reply['frames'][0], {'occupied': 'yes', 'uncertain': False}]}):
            with patch('analysis_pipeline.frame', return_value=('image/jpeg', b'crop')), \
                 patch('analysis_pipeline.structured', return_value=malformed) as model:
                observation = observe_presence('clip.mp4', 12, self.regions)
                self.assertTrue(observation['uncertain'])
                self.assertEqual(observation['uncertain_boxes'], [1, 2])
                self.assertEqual(model.call_count, 3)

    def test_visit_survives_single_missed_sample_but_real_exit_splits_it(self):
        def observations(boxes):
            return [dict(t=i*2, boxes=b, uncertain=False) for i, b in enumerate(boxes)]
        self.assertEqual(intervals(observations([[2], [2], [], [2], [2]])),
                         [dict(box_id=2, first=0, last=8)])
        self.assertEqual(intervals(observations([[2], [2], [], [], [2], [2]])),
                         [dict(box_id=2, first=0, last=2), dict(box_id=2, first=8, last=10)])
        self.assertEqual(intervals(observations([[1], [], [1], [], [1]])), [])
        self.assertEqual(intervals(observations([[1], [1], [2], [2]])),
                         [dict(box_id=1, first=0, last=2), dict(box_id=2, first=4, last=6)])
        uncertain = observations([[2], [2], [2], [2], [2]])
        uncertain[2]['uncertain_boxes'] = [2]
        self.assertEqual(intervals(uncertain), [dict(box_id=2, first=0, last=8)])

    def test_adjacent_recordings_are_one_visit_with_preserved_source_segments(self):
        first_row = self.pipeline.take()
        visit = dict(box_id=2, cat_id='kalinka', first=4, last=64,
                     outcome='uncertain', region=None, feces_region=None, note='Obserwacja')
        result = dict(status='analyzed', visits=[visit], duration_seconds=66.053,
                      presence_method='separate_tray_crops_v1')
        self.pipeline.complete(first_row, result, 'a'*24)
        self.result['recordings'].append(dict(startTime=1790184124, endTime=1790184174))
        self.store.recordings.ingest(self.result, 1790184258)
        second_row = self.pipeline.take()
        continuation = dict(visit, first=0, last=20, outcome='urine', region=4)
        for change in (dict(first=2), dict(cat_id='kefir'), dict(cat_id='unknown'), dict(box_id=1)):
            self.assertIsNone(self.pipeline.continuation(second_row, dict(continuation, **change)))
        for change in (dict(start_epoch=1790184128), dict(camera_host='other')):
            self.assertIsNone(self.pipeline.continuation(dict(second_row, **change), continuation))
        early_exit = dict(result, visits=[dict(visit, last=60)])
        with self.store.db:
            self.store.db.execute('UPDATE camera_recordings SET analysis=? WHERE media_key=?',
                                 (json.dumps(early_exit), 'a'*24))
        self.assertIsNone(self.pipeline.continuation(second_row, continuation))
        with self.store.db:
            self.store.db.execute('UPDATE camera_recordings SET analysis=? WHERE media_key=?',
                                 (json.dumps(result), 'a'*24))
        self.pipeline.complete(second_row, dict(result, visits=[continuation], duration_seconds=50), 'b'*24)
        self.pipeline.complete(second_row, dict(result, visits=[continuation], duration_seconds=50), 'b'*24)
        combined, = self.store.snapshot()['visits']
        self.assertEqual(combined['id'], 'a'*24+'-0')
        self.assertEqual([s['id'] for s in combined['segments']], ['a'*24+'-0', 'b'*24+'-0'])
        self.assertEqual(combined['exited_at'], '2026-09-23T17:22:24+00:00')
        self.assertEqual((combined['outcome'], combined['region']), ('urine', 4))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM visits').fetchone()[0], 2)
        self.store.review(combined['id'], {'outcome': 'confirmed', 'region': 1, 'note': 'Moja ocena'})
        reviewed, = self.store.snapshot()['visits']
        self.assertEqual((reviewed['outcome'], reviewed['region'], reviewed['note']), ('confirmed', 1, 'Moja ocena'))
        reopened = Store(Path(self.temp.name)/'test.sqlite3', seed=False)
        try:
            self.assertEqual(reopened.snapshot()['visits'], [reviewed])
        finally:
            reopened.db.close()

    def test_same_cat_recording_is_one_session_across_trays_and_reviews(self):
        def part(index, cat='kefir', box=1, **fields):
            return dict(id='c'*24+f'-{index}', cat_id=cat, source='camera',
                        entered_at=f'2026-09-24T14:59:{10+index*10:02}+00:00',
                        exited_at=f'2026-09-24T14:59:{20+index*10:02}+00:00',
                        box_id=box, outcome='uncertain', region=None, **fields)
        a, b, c = part(0, box=2), part(1), part(2, box=2)
        a.update(reviewed_at='2026-09-24T15:00:00+00:00')
        b.update(reviewed_at='2026-09-24T15:01:00+00:00', outcome='confirmed', region=4)
        original = json.loads(json.dumps([a, b, c]))
        session, = join_visit_segments([a, b, c])
        self.assertEqual(len(session['segments']), 3)
        self.assertEqual(session['box_ids'], [2, 1])
        self.assertEqual(session['exited_at'], c['exited_at'])
        self.assertEqual((session['outcome'], session['box_id'], session['region']), ('confirmed', 1, 4))
        self.assertEqual([a, b, c], original)
        mixed_identity_session, = join_visit_segments([a, part(1, cat='kalinka'), part(2, cat='unknown')])
        self.assertEqual(len(mixed_identity_session['segments']), 3)
        self.assertEqual(mixed_identity_session['cat_id'], 'kefir')
        other = dict(b, id='d'*24+'-0')
        self.assertEqual(len(join_visit_segments([a, other])), 2)
        a.update(reviewed_at=None, outcome='urine', region=2)
        b.update(reviewed_at=None, outcome='feces', feces_region=5)
        combined, = join_visit_segments([a,b])
        self.assertEqual(combined['outcome'], 'both')
        self.assertIsNone(combined['region'])
        self.assertIsNone(combined['feces_region'])

    def test_reanalysis_refreshes_suggestions_without_overwriting_manual_location(self):
        key = 'e' * 24
        result = {'status': 'analyzed', 'visits': [{'box_id': 1, 'cat_id': 'kalinka',
                  'first': 0, 'last': 20, 'outcome': 'uncertain', 'region': None,
                  'feces_region': None, 'note': 'Model', 'focus': {'suggestions': []}}]}
        self.pipeline.complete(self.pipeline.take(), result, key)
        self.store.review(key + '-0', {'cat_id': 'kalinka', 'outcome': 'confirmed',
                          'region': 3, 'urine_point': [.27, .48], 'note': 'Miejsce ręczne'})
        before = self.store.snapshot()['visits'][0]
        with self.store.db:
            self.store.db.execute("UPDATE camera_recordings SET status='pending_analysis',retry_at=0")
        result['visits'][0]['focus']['suggestions'] = [{'point': [.3, .5], 'region': 3}]
        with patch('analysis_pipeline.time.time', return_value=1799999999):
            self.pipeline.complete(self.pipeline.take(), result, key)
        state = self.store.snapshot()
        self.assertEqual(state['visits'], [before])
        saved = json.loads(state['recordings']['items'][0]['analysis'])
        self.assertGreater(saved['completed_at'], before['reviewed_at'])
        self.assertEqual(saved['visits'][0]['focus']['suggestions'], [{'point': [.3, .5], 'region': 3}])

    def test_posture_suggestions_prefer_longer_stop_and_skip_brief_or_duplicate_regions(self):
        path = Path(self.temp.name) / 'recording.mp4'
        polygon = [[.4,.1],[.6,.1],[.6,.9],[.4,.9]]
        windows = [dict(stationary_since=0, start=2, point=[.42,.2]),
                   dict(stationary_since=4, start=10, point=[.5,.5]),
                   dict(stationary_since=12, start=24, point=[.5,.5])]
        reader = Mock(return_value=('image/jpeg', b'photo'))
        suggestion, = posture_suggestions(path, windows, polygon, 2, reader)
        self.assertEqual((suggestion['t'], suggestion['stationary_seconds'], suggestion['region']), (18, 12, 4))
        self.assertAlmostEqual(suggestion['point'][0], .5)
        self.assertAlmostEqual(suggestion['point'][1], .5)
        reader.assert_called_once_with(path, 18, polygon, max_side=640)

    def test_missing_or_invalid_tail_cannot_create_suggestion_or_evidence(self):
        polygon = [[0, 0], [1, 0], [1, 1], [0, 1]]
        observations = [dict(t=t, boxes=[1], uncertain=False,
                             rear=[{'box_id': 1, 'point': [.5, .8]}]) for t in (2, 4, 6)]
        observations.append(dict(t=8, boxes=[], uncertain=False))
        for pose in (dict(tail_base_clear=False, head_point=None, tail_base_point=None),
                     dict(tail_base_clear=True, head_point=[.5, .8], tail_base_point=[True, .15])):
            with self.subTest(pose=pose), patch('focus_analysis.motion_samples') as motion:
                reader = Mock(return_value=('image/jpeg', b'photo'))
                result = inspect_focus(Path(self.temp.name)/'recording.mp4', observations,
                    {'first': 2, 'last': 6, 'box_id': 1}, polygon, 10, 0, reader,
                    Mock(return_value=pose), waste_result, lambda: False, lambda _: None)
                self.assertEqual(result['suggestions'], [])
                self.assertEqual(result['windows'], [])
                self.assertEqual(result['evidence'], [])
                self.assertFalse(result['pose_checks'][0]['accepted'])
                check = result['pose_checks'][0]
                self.assertEqual(check['tail_base_point'], None if pose['tail_base_point'] == [True, .15] else pose['tail_base_point'])
                self.assertAlmostEqual(check['candidate_point'][0], .5)
                self.assertAlmostEqual(check['candidate_point'][1], .8)
                self.assertEqual((Path(self.temp.name) / check['file']).read_bytes(), b'photo')
                motion.assert_not_called()

    def test_tail_replaces_wrong_candidate_without_claiming_waste(self):
        observations = [dict(t=t, boxes=[1], uncertain=False,
                             rear=[{'box_id': 1, 'point': [.5, .8]}]) for t in (0, 2, 4)]
        observations.append(dict(t=6, boxes=[], uncertain=False))
        for points, expected in (([[.3, .2]] * 3, True),
                                 ([[.3, .2], [.3, .2], [.7, .8]], False),
                                 ([[.3, .2], None, [.3, .2]], False),
                                 ([[.3, .2], [.23, .2], [.37, .2]], False)):
            with self.subTest(points=points):
                model = Mock(side_effect=[dict(tail_base_point=p, tail_base_clear=False) for p in points])
                result = inspect_focus(Path(self.temp.name)/'recording.mp4', observations,
                    {'first': 0, 'last': 4, 'box_id': 1}, [[0,0],[1,0],[1,1],[0,1]], 10, 0,
                    Mock(return_value=('image/jpeg', b'photo')), model,
                    waste_result, lambda: False, lambda _: None)
                self.assertEqual(result['pose_checks'][0]['accepted'], expected)
                self.assertEqual(result['evidence'], [])
                if expected:
                    self.assertEqual(result['windows'][0]['point'], [.3, .2])
                    self.assertEqual(result['suggestions'][0]['point'], [.3, .2])
                    self.assertLess(max(p[1] for p in result['windows'][0]['crop']), .5)
                else:
                    self.assertEqual(result['windows'], [])
                    self.assertEqual(result['suggestions'], [])

    def test_invalid_anatomy_response_keeps_candidate_for_review_only(self):
        observations = [dict(t=t, boxes=[1], uncertain=False,
                             rear=[{'box_id': 1, 'point': [.5, .8]}]) for t in (2, 4, 6)]
        observations.append(dict(t=8, boxes=[], uncertain=False))
        with patch('focus_analysis.motion_samples') as motion:
            result = inspect_focus(Path(self.temp.name)/'recording.mp4', observations,
                {'first': 2, 'last': 6, 'box_id': 1}, [[0,0],[1,0],[1,1],[0,1]], 10, 0,
                Mock(return_value=('image/jpeg', b'photo')), Mock(side_effect=ValueError('bad JSON')),
                waste_result, lambda: False, lambda _: None)
        self.assertEqual(result['pose_checks'][0]['reason'], 'invalid_response')
        self.assertIsNone(result['pose_checks'][0]['tail_base_point'])
        self.assertEqual(result['windows'], [])
        self.assertEqual(result['suggestions'], [])
        motion.assert_not_called()

    def test_tail_outside_calibrated_tray_is_not_exposed_as_landmark(self):
        polygon = [[.5,.1],[.9,.5],[.5,.9],[.1,.5]]
        observations = [dict(t=t, boxes=[1], uncertain=False,
                             rear=[{'box_id': 1, 'point': [.5, .5]}]) for t in (0, 2, 4)]
        observations.append(dict(t=6, boxes=[], uncertain=False))
        for point in ([.5, 0], [.05, .05], [.5, .5]):
            with self.subTest(point=point):
                result = inspect_focus(Path(self.temp.name)/'recording.mp4', observations,
                    {'first': 0, 'last': 4, 'box_id': 1}, polygon, 10, 0,
                    Mock(return_value=('image/jpeg', b'photo')),
                    Mock(return_value={'tail_base_point': point, 'tail_base_clear': True}),
                    waste_result, lambda: False, lambda _: None)
                check = result['pose_checks'][0]
                if point == [.5, .5]:
                    self.assertEqual(check['tail_base_point'], point)
                    self.assertTrue(check['accepted'])
                else:
                    self.assertIsNone(check['tail_base_point'])
                    self.assertEqual(check['rejected_tail_point'], point)
                    self.assertEqual(check['reason'], 'outside_tray')
                    self.assertFalse(check['accepted'])
                    self.assertEqual(result['windows'], [])
                    self.assertEqual(result['suggestions'], [])

    def test_visible_tail_base_is_accepted_without_a_visible_head(self):
        observations = [dict(t=t, boxes=[1], uncertain=False,
                             rear=[{'box_id': 1, 'point': [.3, .4]}]) for t in (0, 2, 4)]
        observations.append(dict(t=6, boxes=[], uncertain=False))
        for head in (None, [.3, .4]):
            with self.subTest(irrelevant_head=head):
                result = inspect_focus(Path(self.temp.name)/'recording.mp4', observations,
                    {'first': 0, 'last': 4, 'box_id': 1}, [[0,0],[1,0],[1,1],[0,1]], 10, 0,
                    Mock(return_value=('image/jpeg', b'photo')),
                    Mock(return_value={'tail_base_point': [.3, .4], 'tail_base_clear': True,
                                       'head_point': head}), waste_result, lambda: False, lambda _: None)
                self.assertTrue(result['pose_checks'][0]['accepted'])
                self.assertEqual(len(result['suggestions']), 1)
                self.assertEqual(result['evidence'], [])

    def test_real_visit_commit_is_atomic_idempotent_and_survives_poll(self):
        row = self.pipeline.take()
        result = {'status': 'analyzed', 'visits': [{'box_id': 1, 'first': 4, 'last': 20,
                   'outcome': 'uncertain', 'region': None, 'feces_region': None, 'note': 'Obserwacja próbna'}]}
        self.pipeline.complete(row, result, 'a' * 24)
        notices = len(self.store.snapshot()['notices'])
        self.pipeline.complete(row, result, 'a' * 24)
        self.store.recordings.ingest(self.result, 1790184358)
        state = self.store.snapshot()
        self.assertEqual(len(state['visits']), 1)
        self.assertEqual(state['visits'][0]['entered_at'], '2026-09-23T17:21:02+00:00')
        self.assertEqual(state['visits'][0]['cat_id'], 'unknown')
        self.assertEqual(state['visits'][0]['source'], 'camera')
        self.assertEqual(len(state['notices']), notices)
        self.assertEqual(state['recordings']['items'][0]['status'], 'analyzed')
        self.assertIsNone(self.pipeline.take())
        self.store.review(state['visits'][0]['id'], {'cat_id': 'kefir', 'outcome': 'uncertain', 'note': 'To Kefir'})
        self.assertEqual(self.store.snapshot()['visits'][0]['cat_id'], 'kefir')

    def test_growing_clip_discards_stale_analysis(self):
        row = self.pipeline.take()
        self.result['recordings'][0]['endTime'] += 10
        self.store.recordings.ingest(self.result, 1790184358)
        self.pipeline.complete(row, {'status': 'no_cat_observed', 'visits': []}, 'b' * 24)
        self.assertEqual(self.store.snapshot()['visits'], [])
        self.assertEqual(self.store.recordings.snapshot()['items'][0]['status'], 'pending_analysis')

    def test_detected_identity_is_saved_and_does_not_overwrite_manual_review(self):
        row = self.pipeline.take()
        result = {'status': 'analyzed', 'visits': [{'box_id': 1, 'first': 4, 'last': 20,
                   'cat_id': 'kalinka', 'outcome': 'uncertain', 'region': None,
                   'feces_region': None, 'note': 'Prawdopodobnie Kalinka'}]}
        key = 'd' * 24
        self.pipeline.complete(row, result, key)
        visit = self.store.snapshot()['visits'][0]
        self.assertEqual(visit['cat_id'], 'kalinka')
        self.assertIn('Kalinka', self.store.snapshot()['notices'][0]['text'])
        self.store.review(visit['id'], {'cat_id': 'kefir', 'outcome': 'uncertain', 'note': 'To Kefir'})
        self.pipeline.complete(row, result, key)
        self.assertEqual(self.store.snapshot()['visits'][0]['cat_id'], 'kefir')

    def test_human_only_frames_do_not_create_visits(self):
        with patch('analysis_pipeline.probe', return_value=10), \
             patch('analysis_pipeline.frame', return_value=('image/jpeg', b'frame')), \
             patch('analysis_pipeline.observe_presence', return_value=
                 {'cat_visible': False, 'boxes': [], 'uncertain': False}):
            result = analyze_recording('unused.mp4', self.regions)
        self.assertEqual(result['status'], 'no_cat_observed')
        self.pipeline.complete(self.pipeline.take(), result, 'c' * 24)
        self.assertEqual(self.store.snapshot()['visits'], [])

    def test_visible_cat_visit_does_not_analyze_waste(self):
        presence = {'frames': [{'cat_visible': bool(boxes), 'boxes': boxes, 'uncertain': False}
                                for boxes in ([], [1], [1], [], [])]}
        waste = {'before_after_clear': True, 'urine': True, 'feces': True,
                 'urine_region': 0, 'feces_region': 8, 'notes': 'Nowe zmiany przed/po'}
        with patch('analysis_pipeline.probe', return_value=10), \
             patch('analysis_pipeline.frame', return_value=('image/jpeg', b'frame')), \
             patch('analysis_pipeline.observe_presence', side_effect=presence['frames']), \
             patch('analysis_pipeline.structured', side_effect=AssertionError('Unexpected waste analysis')) as model:
            result = analyze_recording('unused.mp4', self.regions)
        visit = result['visits'][0]
        self.assertEqual((visit['first'], visit['last'], visit['outcome']), (2, 4, 'uncertain'))
        self.assertEqual((visit['region'], visit['feces_region']), (None, None))
        model.assert_not_called()
        self.assertEqual(visit['cat_id'], 'unknown')
        waste['before_after_clear'] = False
        self.assertEqual(waste_result(waste)['outcome'], 'uncertain')
        self.assertIsNone(waste_result(waste)['region'])

    def test_visit_identity_is_independent_of_waste_evidence(self):
        presence = [{'frames': [{'cat_visible': bool(boxes), 'boxes': boxes, 'uncertain': False}]}
                    for boxes in ([], [1], [1], [1], [], [])]
        waste = {'before_after_clear': False, 'urine': False, 'feces': False,
                 'urine_region': None, 'feces_region': None, 'notes': 'Brak widocznego śladu'}
        identity = {'cat_id': 'kefir', 'reason': 'Zgodna sylwetka i ogon'}
        with patch('analysis_pipeline.probe', return_value=12), \
             patch('analysis_pipeline.frame', return_value=('image/jpeg', b'frame')), \
             patch('analysis_pipeline.observe_presence', side_effect=[p['frames'][0] for p in presence]), \
             patch('analysis_pipeline.structured', side_effect=[waste]), \
             patch('analysis_pipeline.identify_visit', return_value=identity) as identify:
            result = analyze_recording('unused.mp4', self.regions, identity_profiles='manifest.json')
        visit = result['visits'][0]
        self.assertEqual((visit['cat_id'], visit['outcome']), ('kefir', 'uncertain'))
        self.assertIsNone(visit['region'])
        self.assertEqual(visit['identity'], identity)
        self.assertEqual(identify.call_args.args[3], 'manifest.json')

    def test_bad_model_response_fails_without_fabricating_observations(self):
        for response in ({'frames': []}, {'frames': [{'cat_visible': False, 'boxes': [1], 'uncertain': False}]},
                         {'frames': [{'cat_visible': True, 'boxes': [3], 'uncertain': False}]}):
            with self.assertRaises(ValueError):
                validate_presence(response, 1)
        self.assertEqual(intervals([{'t': 0, 'boxes': [1], 'uncertain': False}]), [])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_portrait_comparison_frames_fit_context_without_upscaling_small_crops(self):
        path = Path(self.temp.name) / 'frame.ppm'
        path.write_bytes(b'P6\n1600 1200\n255\n' + b'\x80\x90\xa0' * (1600 * 1200))
        for crop in ([[.1,0],[.3,0],[.3,1],[.1,1]],
                     [[.1,.1],[.2,.1],[.2,.2],[.1,.2]]):
            with self.subTest(crop=crop):
                mime, data = frame(path, 0, crop)
                result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                    'stream=width,height', '-of', 'json', '-'], input=data,
                    capture_output=True, check=True, timeout=30)
                dimensions = json.loads(result.stdout)['streams'][0]
                left, top, right, bottom = bounds(crop)
                self.assertEqual(mime, 'image/jpeg')
                self.assertLessEqual(dimensions['width'], min(1024, int((right-left)*1600)))
                self.assertLessEqual(dimensions['height'], min(1024, int((bottom-top)*1200)))

    def test_legacy_tail_observations_cannot_trigger_anatomy_or_waste_analysis(self):
        observations = [dict(cat_visible=True, boxes=[1], uncertain=False,
                             rear=[{'box_id': 1, 'point': point}])
                        for point in ([.2, .5], [.2, .5], [.35, .8])]
        with patch('analysis_pipeline.probe', return_value=6), \
             patch('analysis_pipeline.observe_presence', side_effect=observations), \
             patch('analysis_pipeline.structured') as model, \
             patch('focus_analysis.inspect_focus') as focus:
            result = analyze_recording('unused.mp4', self.regions)
        model.assert_not_called()
        focus.assert_not_called()
        visit, = result['visits']
        self.assertEqual(visit['outcome'], 'uncertain')
        self.assertEqual(visit['focus'], {'enabled': False})
        self.assertIsNone(visit['region'])
        self.assertIsNone(visit['feces_region'])

    def test_invalid_rear_locations_and_motion_do_not_create_stationary_area(self):
        polygon = self.regions['boxes'][0]['points']
        for point in ([float('nan'), .5], [True, .5], [.9, .5], None, [0]):
            self.assertIsNone(rear_point({'boxes': [1], 'rear': [{'box_id': 1, 'point': point}]}, 1, polygon))
        observations = [{'t': t, 'boxes': [1], 'uncertain': False,
                         'rear': [{'box_id': 1, 'point': p}]} for t, p in [(2, [.1,.2]), (4, [.3,.8])]]
        self.assertEqual(focus_windows(observations, {'box_id': 1, 'first': 2, 'last': 4}, polygon, 10), [])
        self.assertEqual(tray_region(4, [[.05,.05],[.1,.05],[.1,.1],[.05,.1]], polygon), 0)

    def test_interrupted_job_requeues_on_restart_without_visits(self):
        self.pipeline.take()
        with patch.object(self.pipeline.thread, 'start'):
            self.pipeline.start()
        row = self.store.recordings.snapshot()['items'][0]
        self.assertEqual((row['status'], row['attempts']), ('pending_analysis', 0))
        self.assertEqual(self.store.snapshot()['visits'], [])

    def test_model_outage_downloads_backlog_survives_restart_and_recovers_in_order(self):
        second_start = self.result['recordings'][0]['startTime'] + 100
        self.result['recordings'].append({'startTime': second_start, 'endTime': second_start + 60})
        self.store.recordings.ingest(self.result, second_start + 200)
        now = [1800000000.0]
        downloaded = []

        def download(job, timeout):
            data = json.loads(job)
            Path(data['target']).write_bytes(b'video')
            downloaded.append(data['start_epoch'])
            return ('{"ok":true}', '')

        process = Mock(returncode=0)
        process.communicate.side_effect = download
        with patch('analysis_pipeline.time.time', side_effect=lambda: now[0]), \
             patch('analysis_pipeline.probe', return_value=60), \
             patch('analysis_pipeline.subprocess.Popen', return_value=process), \
             patch('analysis_pipeline.analyze_recording', side_effect=ModelUnavailable()) as model:
            for _ in range(2):
                row = self.pipeline.take()
                with self.assertRaises(ModelUnavailable):
                    self.pipeline.process(row)
                self.pipeline.wait_for_model(row)
            self.assertEqual(model.call_count, 1)  # Second film downloaded without another model call.
            self.assertIsNone(self.pipeline.take())
        self.assertEqual(downloaded, sorted(downloaded))
        self.assertEqual(len(downloaded), 2)
        self.assertTrue(all(r['attempts'] == 0 and r['status'] == 'waiting_model'
                            for r in self.store.recordings.snapshot()['items']))

        # Reopen the database, as on an application restart during the outage.
        db_path = Path(self.temp.name) / 'test.sqlite3'
        self.store.db.close()
        self.store = Store(db_path, seed=False)
        self.pipeline = AnalysisPipeline(self.store, self.temp.name)
        with patch.object(self.pipeline.thread, 'start'):
            self.pipeline.start()
        self.assertEqual(self.pipeline.model_retry_at, now[0]+60)
        calls = []

        def analyze(path, *args, **kwargs):
            calls.append(path.parent.name)
            if len(calls) <= 4:
                raise ModelUnavailable()
            return {'status': 'no_cat_observed', 'visits': []}

        def wait(_):
            if len(calls) >= 6:
                self.pipeline.stop.set()
            else:
                now[0] += 60

        with patch('analysis_pipeline.time.time', side_effect=lambda: now[0]), \
             patch('analysis_pipeline.probe', return_value=60), \
             patch('analysis_pipeline.subprocess.Popen') as download_again, \
             patch('analysis_pipeline.analyze_recording', side_effect=analyze), \
             patch.object(self.pipeline.stop, 'wait', side_effect=wait):
            self.pipeline.run()
        self.assertFalse(download_again.called)
        keys = [hashlib.sha256(f"192.0.2.1:{r['startTime']}:{r['endTime']}".encode()).hexdigest()[:24]
                for r in self.result['recordings']]
        self.assertEqual(calls, [keys[0]] * 5 + [keys[1]])
        self.assertTrue(all(r['status'] == 'no_cat_observed' and r['attempts'] == 1
                            for r in self.store.recordings.snapshot()['items']))
        self.assertEqual(self.store.snapshot()['visits'], [])

    def test_bad_film_still_stops_after_three_attempts(self):
        now = [1800000000.0]

        def wait(_):
            if self.store.recordings.snapshot()['items'][0]['status'] == 'failed':
                self.pipeline.stop.set()
            else:
                now[0] += 300

        with patch('analysis_pipeline.time.time', side_effect=lambda: now[0]), \
             patch.object(self.pipeline, 'process', side_effect=ValueError('Bad video')), \
             patch.object(self.pipeline.stop, 'wait', side_effect=wait):
            self.pipeline.run()
        row = self.store.recordings.snapshot()['items'][0]
        self.assertEqual((row['status'], row['attempts']), ('failed', 3))

    def test_download_errors_identify_stage_without_exposing_raw_details(self):
        row = self.pipeline.take()
        for stage, code, expected in [('camera_connection', 'camera_error', 'zalogować'),
                                      ('camera_archive', 'camera_error', 'archiwum'),
                                      ('camera_download', 'no_media', 'Brak poprawnej lokalnej kopii'),
                                      ('camera_download', 'timeout', 'czas oczekiwania')]:
            with self.subTest(stage=stage, code=code):
                process = Mock(returncode=1)
                process.communicate.return_value = (json.dumps({'ok': False, 'error_stage': stage,
                    'error_code': code, 'error': 'secret-password'}), 'secret-password')
                with patch('analysis_pipeline.subprocess.Popen', return_value=process), \
                     patch('analysis_pipeline.analyze_recording') as analyze:
                    with self.assertRaises(ProcessingError) as caught:
                        self.pipeline.process(row)
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn('secret-password', str(caught.exception))
                analyze.assert_not_called()

    def test_deleted_camera_recording_is_not_analyzed_or_retried(self):
        row = self.pipeline.take()
        process = Mock(returncode=1)
        process.communicate.return_value = ('{"ok":false,"error_code":"recording_missing"}', '')
        with patch('analysis_pipeline.subprocess.Popen', return_value=process), \
             patch('analysis_pipeline.analyze_recording') as analyze:
            self.pipeline.process(row)
        analyze.assert_not_called()
        self.assertEqual(self.store.recordings.snapshot()['items'][0]['status'], 'missing_on_camera')
        self.assertIsNone(self.pipeline.take())
        self.assertEqual(self.store.snapshot()['visits'], [])

    def test_incomplete_download_is_rejected_before_analysis(self):
        row = self.pipeline.take()
        process = Mock(returncode=0)

        def downloaded(job, timeout):
            Path(json.loads(job)['target']).write_bytes(b'incomplete-video')
            return ('{"ok":true}', '')

        process.communicate.side_effect = downloaded
        with patch('analysis_pipeline.subprocess.Popen', return_value=process), \
             patch('analysis_pipeline.probe', side_effect=ValueError('Incomplete recording')), \
             patch('analysis_pipeline.analyze_recording') as analyze:
            with self.assertRaisesRegex(ProcessingError, 'Pobrany film jest niekompletny'):
                self.pipeline.process(row)
        analyze.assert_not_called()
        self.assertEqual(list(Path(self.temp.name).rglob('recording.mp4')), [])

    def test_analysis_failure_preserves_download_and_reports_distinct_stage(self):
        row = self.pipeline.take()
        key = hashlib.sha256(f"{row['camera_host']}:{row['start_epoch']}:{row['end_epoch']}".encode()).hexdigest()[:24]
        path = Path(self.temp.name) / 'data' / 'recordings' / 'processed' / key / 'recording.mp4'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'valid-video')
        with patch('analysis_pipeline.probe', return_value=66), \
             patch('analysis_pipeline.analyze_recording', side_effect=ValueError('secret-password')), \
             patch('analysis_pipeline.subprocess.Popen') as download:
            with self.assertRaises(ProcessingError) as caught:
                self.pipeline.process(row)
        self.assertIn('Film pobrany i zachowany. Błąd analizy', str(caught.exception))
        self.assertNotIn('secret-password', str(caught.exception))
        self.assertEqual(path.read_bytes(), b'valid-video')
        download.assert_not_called()

    def test_recording_extended_during_model_outage_stays_in_queue(self):
        self.pipeline.wait_for_model(self.pipeline.take())
        self.store.recordings.ingest(self.result, 1790184358)
        self.assertEqual(self.store.recordings.snapshot()['items'][0]['status'], 'waiting_model')
        self.result['recordings'][0]['endTime'] += 10
        self.store.recordings.ingest(self.result, 1790184358)
        row = self.store.recordings.snapshot()['items'][0]
        self.assertEqual(row['status'], 'pending_analysis')
        self.assertEqual(row['end_epoch'], self.result['recordings'][0]['endTime'])


if __name__ == '__main__':
    unittest.main()
