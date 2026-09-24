import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from opencv_presence import changed_fraction, observe_recording, scan_motion
from analysis_pipeline import analyze_recording, intervals, structured
from qwen import ModelUnavailable

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = np = None


REGIONS = {'boxes': [dict(id=1, points=[[.05,.05],[.45,.05],[.45,.45],[.05,.45]]),
                     dict(id=2, points=[[.55,.05],[.95,.05],[.95,.45],[.55,.45]])]}


def observation(boxes, **fields):
    return dict(cat_visible=bool(boxes), boxes=boxes, uncertain=False, uncertain_boxes=[],
                rear=[{'box_id': b, 'point': [.2, .2]} for b in boxes], **fields)


class SamplingTests(unittest.TestCase):
    def test_complete_qwen_object_with_stray_quote_but_not_conflicting_answers(self):
        with patch('analysis_pipeline.analyze_images', return_value='{"frames": []}"'):
            self.assertEqual(structured([], 'test'), {'frames': []})
        for response in ('{"frames": []}{"frames": [1]}', '{"frames": []} ignore this', '{"frames": ['):
            with patch('analysis_pipeline.analyze_images', return_value=response), self.assertRaises(ValueError):
                structured([], 'test')

    def scan(self, count, selected):
        return dict(samples=[dict(t=i*2, keyframe=i in selected) for i in range(count)],
                    selected=selected, scan_seconds=.1)

    def test_stationary_cat_is_kept_but_landmarks_are_not_copied(self):
        with patch('opencv_presence.scan_motion', return_value=self.scan(6, [0,5])):
            calls = []
            def observe(p, t, r):
                calls.append(t)
                return observation([1])
            result, stats = observe_recording('unused', REGIONS, list(range(0,12,2)), observe)
        self.assertEqual(calls, [0,10])
        self.assertEqual(intervals(result), [dict(box_id=1, first=0, last=10)])
        self.assertEqual(stats['reused_presence_frames'], 4)
        self.assertTrue(result[0]['rear'])
        self.assertTrue(all(not o['rear'] for o in result[1:-1]))

    def test_transition_is_refined_without_making_both_trays_occupied(self):
        with patch('opencv_presence.scan_motion', return_value=self.scan(6, [0,5])):
            result, stats = observe_recording('unused', REGIONS, list(range(0,12,2)),
                lambda p,t,r: observation([1] if t<4 else [2]))
        self.assertEqual(stats['qwen_presence_frames'], 6)
        self.assertEqual(intervals(result), [dict(box_id=1, first=0, last=2),
                                            dict(box_id=2, first=4, last=10)])

    def test_uncertainty_does_not_fill_a_visit_and_model_outage_propagates(self):
        with patch('opencv_presence.scan_motion', return_value=self.scan(6, [0,5])):
            def uncertain(p,t,r):
                result = observation([1]); result['uncertain_boxes'] = [1]
                return result
            result, stats = observe_recording('unused', REGIONS, list(range(0,12,2)), uncertain)
            self.assertEqual(stats['qwen_presence_frames'], 6)
            self.assertEqual(intervals(result), [])
            with self.assertRaises(ModelUnavailable):
                observe_recording('unused', REGIONS, list(range(0,12,2)),
                                  lambda *args: (_ for _ in ()).throw(ModelUnavailable()))

    def test_motion_is_never_a_cat_and_pipeline_records_backend(self):
        with patch('analysis_pipeline.probe', return_value=6), \
             patch('opencv_presence.scan_motion', return_value=self.scan(3,[0,1,2])), \
             patch('analysis_pipeline.observe_presence', return_value=observation([])):
            result = analyze_recording('unused', REGIONS, presence_engine='opencv')
        self.assertEqual(result['status'], 'no_cat_observed')
        self.assertEqual(result['visits'], [])
        self.assertEqual(result['presence_stats']['qwen_presence_frames'], 3)
        self.assertEqual(result['presence_method'], 'opencv_verified_presence_v1')


@unittest.skipUnless(cv2 is not None and shutil.which('ffmpeg'), 'Install requirements-opencv.txt and FFmpeg')
class VisionTests(unittest.TestCase):
    def test_polygon_mask_ignores_surroundings_and_small_exposure_changes(self):
        a = np.full((80,80), 80, np.uint8)
        mask = np.zeros_like(a); cv2.fillPoly(mask, [np.array([[40,5],[75,40],[40,75],[5,40]])], 1)
        b = a.copy(); b[mask==0] = 240
        self.assertEqual(changed_fraction(a,b,mask), 0)
        self.assertEqual(changed_fraction(a,a+5,mask), 0)
        b = a.copy(); b[25:55,25:55] = 180
        self.assertGreater(changed_fraction(a,b,mask), .2)
        self.assertGreater(changed_fraction(a,a+50,mask), .9)

    def test_real_video_motion_entry_exit_and_tapo_stream_start_offset(self):
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d)/'raw.avi'; movie = Path(d)/'offset.mp4'
            writer = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*'MJPG'), 2, (160,120))
            self.assertTrue(writer.isOpened())
            for index in range(14):
                frame = np.full((120,160,3), 70, np.uint8)
                if 4 <= index < 10:
                    frame[12:45,12:55] = 230
                # Irrelevant activity outside both trays.
                frame[85:110, index*8:index*8+10] = 255
                writer.write(frame)
            writer.release()
            subprocess.run(['ffmpeg','-v','error','-itsoffset','2','-i',str(raw),
                            '-f','lavfi','-i','anullsrc=r=8000:cl=mono','-t','9',
                            '-c:v','libx264','-fps_mode','passthrough','-c:a','aac',str(movie)],
                           capture_output=True, check=True, timeout=30)
            result = scan_motion(movie, REGIONS, list(range(9)))
            # Full timeline includes the late final frames despite the 2 s
            # audio/video start offset; old POS_MSEC seeking fails at 7 s.
            self.assertEqual(len(result['samples']),9)
            self.assertGreater(result['samples'][4]['motion'][0], .1)
            self.assertGreater(result['samples'][7]['motion'][0], .1)
            self.assertTrue(all(o['motion'][1] < .01 for o in result['samples']))
            self.assertIn(4, result['selected'])
            self.assertIn(7, result['selected'])
            with self.assertRaises(InterruptedError):
                scan_motion(movie, REGIONS, [0], stopped=lambda: True)


if __name__ == '__main__':
    unittest.main()
