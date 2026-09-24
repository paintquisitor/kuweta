import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from server import Store, create_server
from qwen import ModelUnavailable, analyze_images
from camera import CameraPreview, camera_settings, stream_url


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "test.sqlite3")
        self.server = create_server(self.path, port=0, seed=False, simulation_seconds=0.08)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server.store.db.close()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base + path, data=data,
                          headers=headers or ({"Content-Type": "application/json"} if data else {}))
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.read()

    def state(self):
        return json.loads(self.request("/api/state")[1])

    def test_recording_video_range_and_unknown_file_access(self):
        key = 'a' * 24
        path = Path(self.temp.name) / 'data' / 'recordings' / 'processed' / key / 'recording.mp4'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'0123456789')
        (path.parent / 'visit-0-urine.jpg').write_bytes(b'jpeg-evidence')
        (path.parent / 'visit-1-urine.jpg').write_bytes(b'unpublished')
        (path.parent / 'visit-0-posture-0.jpg').write_bytes(b'posture-photo')
        (path.parent / 'visit-0-posture-1.jpg').write_bytes(b'unpublished')
        (path.parent / 'visit-0-anatomy-0.jpg').write_bytes(b'anatomy-photo')
        (path.parent / 'visit-0-anatomy-1.jpg').write_bytes(b'unpublished')
        self.server.store.recordings.ingest({'camera_host': '192.0.2.1',
            'recordings': [{'startTime': 1000, 'endTime': 1010}], 'checked_through': 2000}, 2000)
        with self.server.store.lock, self.server.store.db:
            analysis = {'visits': [{'focus': {'evidence': [{'file': 'visit-0-urine.jpg'}],
                        'suggestions': [{'file': 'visit-0-posture-0.jpg'}],
                        'pose_checks': [{'file': 'visit-0-anatomy-0.jpg', 'accepted': False}]}}]}
            self.server.store.db.execute('UPDATE camera_recordings SET media_key=?,analysis=?', (key, json.dumps(analysis)))
        with patch('server.ROOT', Path(self.temp.name)):
            self.assertEqual(self.request(f'/api/recordings/{key}/video', headers={'Range': 'bytes=2-5'}), (206, b'2345'))
            self.assertEqual(self.request(f'/api/recordings/{key}/video', headers={'Range': 'bytes=-3'}), (206, b'789'))
            self.assertEqual(self.request(f'/api/recordings/{key}/video', headers={'Range': 'bytes=50-60'})[0], 416)
            self.assertEqual(self.request('/api/recordings/' + 'b' * 24 + '/video')[0], 404)
            self.assertEqual(self.request(f'/api/recordings/{key}/evidence/visit-0-urine.jpg'), (200, b'jpeg-evidence'))
            self.assertEqual(self.request(f'/api/recordings/{key}/evidence/visit-1-urine.jpg')[0], 404)
            self.assertEqual(self.request(f'/api/recordings/{key}/evidence/visit-0-posture-0.jpg'), (200, b'posture-photo'))
            self.assertEqual(self.request(f'/api/recordings/{key}/evidence/visit-0-posture-1.jpg')[0], 404)
            self.assertEqual(self.request(f'/api/recordings/{key}/evidence/visit-0-anatomy-0.jpg'), (200, b'anatomy-photo'))
            self.assertEqual(self.request(f'/api/recordings/{key}/evidence/visit-0-anatomy-1.jpg')[0], 404)
            self.assertEqual(self.request(f'/api/recordings/{key}/evidence/../../.env')[0], 404)

        # Identical recording IDs in a new database must serve its own evidence.
        experiment = Path(self.temp.name) / 'experiment' / key
        experiment.mkdir(parents=True)
        (experiment / 'recording.mp4').write_bytes(b'experiment-video')
        (experiment / 'visit-0-urine.jpg').write_bytes(b'new-evidence')
        with patch('server.ROOT', Path(self.temp.name)), \
             patch.dict(os.environ, {'KUWETA_RECORDINGS_DIR': 'experiment'}):
            self.assertEqual(self.request(f'/api/recordings/{key}/video'), (200, b'experiment-video'))
            self.assertEqual(self.request(f'/api/recordings/{key}/evidence/visit-0-urine.jpg'), (200, b'new-evidence'))
            self.assertEqual(self.request(f'/api/recordings/{key}/evidence/visit-0-posture-0.jpg')[0], 404)
        self.assertEqual((path.parent / 'visit-0-urine.jpg').read_bytes(), b'jpeg-evidence')

    def test_tail_labels_are_separate_persistent_and_bound_to_exact_frame(self):
        key = 'c' * 24
        visit_id = key + '-0'
        name = 'visit-0-anatomy-0.jpg'
        image = Path(self.temp.name) / 'data' / 'recordings' / 'processed' / key / name
        image.parent.mkdir(parents=True)
        image.write_bytes(b'original-frame')
        store = self.server.store
        store.recordings.ingest({'camera_host': '192.0.2.1',
            'recordings': [{'startTime': 1000, 'endTime': 1010}], 'checked_through': 2000}, 2000)
        analysis = json.dumps({'visits': [{'focus': {'pose_checks': [
            {'file': name, 't': 8, 'tail_base_point': [.7, .3]}]}}]})
        with store.db:
            store.db.execute('UPDATE camera_recordings SET media_key=?,analysis=?', (key, analysis))
            store.db.execute("INSERT INTO visits (id,cat_id,box_id,entered_at,status,outcome,scenario,source) "
                             "VALUES (?, 'kalinka', 1, '2026-09-24', 'done', 'uncertain', 'camera', 'camera')", (visit_id,))
        endpoint = f'/api/visits/{visit_id}/tail-labels'
        with patch('server.ROOT', Path(self.temp.name)):
            frame = json.loads(self.request(endpoint)[1])['frames'][0]
            self.assertIsNone(frame['point'])
            body = {'file': name, 'image_sha': frame['image_sha'], 'point': [.4,.25]}
            self.assertEqual(self.request(endpoint, body)[0], 200)
            reopened = Store(self.path, seed=False)
            try:
                self.assertEqual(reopened.tail_frames(visit_id)['frames'][0]['point'], [.4,.25])
            finally:
                reopened.db.close()
            # This workflow must never rewrite Qwen results or confirm waste.
            self.assertEqual(store.db.execute('SELECT analysis FROM camera_recordings').fetchone()[0], analysis)
            visit = dict(store.db.execute('SELECT * FROM visits WHERE id=?', (visit_id,)).fetchone())
            self.assertEqual(visit['outcome'], 'uncertain')
            self.assertIsNone(visit['reviewed_at'])
            self.assertIsNone(visit['urine_point'])
            self.assertEqual(store.db.execute('SELECT count(*) FROM reviews').fetchone()[0], 0)
            for point in ([True,.2], [float('nan'),.2], [1.1,.2], [0], 'bad'):
                self.assertEqual(self.request(endpoint, {**body, 'point': point})[0], 400)
            self.assertEqual(self.request(endpoint, {**body, 'file': '../other.jpg'})[0], 400)
            self.assertEqual(self.request('/api/visits/' + 'd' * 24 + '-0/tail-labels', body)[0], 400)
            image.write_bytes(b'regenerated-frame')
            self.assertIsNone(json.loads(self.request(endpoint)[1])['frames'][0]['point'])
            self.assertEqual(self.request(endpoint, body)[0], 400)
            body['image_sha'] = json.loads(self.request(endpoint)[1])['frames'][0]['image_sha']
            self.assertEqual(self.request(endpoint, body)[0], 200)
            self.assertEqual(self.request(endpoint, {**body, 'point': None})[0], 200)
            self.assertIsNone(json.loads(self.request(endpoint)[1])['frames'][0]['point'])

    def test_camera_regions_persist_and_reject_invalid_geometry(self):
        boxes = [{"id": 1, "points": [[.1,.1],[.4,.1],[.4,.8],[.1,.8]]},
                 {"id": 2, "points": [[.5,.1],[.9,.1],[.9,.8],[.5,.8]]}]
        self.assertEqual(json.loads(self.request("/api/camera/regions")[1])["boxes"], [])
        self.assertEqual(self.request("/api/camera/regions", {"boxes": boxes})[0], 200)
        reopened = Store(self.path, seed=False)
        try:
            self.assertEqual(reopened.camera_regions()["boxes"], boxes)
        finally:
            reopened.db.close()
        for invalid in ([[0,0],[1,1],[1,0],[0,1]], [[0,0],[0,0],[0,0],[0,0]],
                        [[-1,0],[1,0],[1,1],[0,1]], [[True,0],[1,0],[1,1],[0,1]],
                        [[float('nan'),0],[1,0],[1,1],[0,1]]):
            bad = [{"id": 1, "points": invalid}, boxes[1]]
            self.assertEqual(self.request("/api/camera/regions", {"boxes": bad})[0], 400)
        self.assertEqual(self.request("/api/camera/regions", {"boxes": [boxes[0], boxes[0]]})[0], 400)
        self.assertEqual(json.loads(self.request("/api/camera/regions")[1])["boxes"], boxes)

    def test_camera_preview_and_stale_frame(self):
        camera = CameraPreview(Path(self.temp.name) / ".env")
        self.server.camera = camera
        self.assertEqual(self.request("/api/camera/frame.jpg")[0], 503)
        camera.frame = b"\xff\xd8sample\xff\xd9"
        camera.received = time.monotonic()
        camera.captured_at = "2026-09-23T10:00:00+00:00"
        self.assertTrue(json.loads(self.request("/api/camera/status")[1])["online"])
        self.assertEqual(self.request("/api/camera/frame.jpg"), (200, camera.frame))
        camera.received -= 11
        self.assertFalse(json.loads(self.request("/api/camera/status")[1])["online"])
        self.assertEqual(self.request("/api/camera/frame.jpg")[0], 503)

    def test_camera_credentials_reload_and_url_encoding(self):
        env = Path(self.temp.name) / ".env"
        env.write_text("CAMERA_HOST=192.168.0.107\nCAMERA_USERNAME=test@user\nCAMERA_PASSWORD=\"a#b:c /\"\n")
        self.assertEqual(stream_url(camera_settings(env)),
                         "rtsp://test%40user:a%23b%3Ac%20%2F@192.168.0.107:554/stream1")
        env.write_text("CAMERA_HOST=192.168.0.107\nCAMERA_USERNAME=test\nCAMERA_PASSWORD=new\n")
        self.assertEqual(camera_settings(env)[2], "new")

    def simulate(self, scenario="urine", cat="kefir", box=1, region=2):
        status, body = self.request("/api/simulate", {"cat_id": cat, "box_id": box,
                                                     "scenario": scenario, "region": region})
        self.assertEqual(status, 201, body)
        return json.loads(body)["visit_id"]

    def finished(self, visit_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            visit = next(v for v in self.state()["visits"] if v["id"] == visit_id)
            if visit["status"] == "done":
                return visit
            time.sleep(0.02)
        self.fail("Simulation did not finish")

    def test_complete_visit_persists_identity_region_and_timestamps(self):
        visit_id = self.simulate()
        visit = self.finished(visit_id)
        self.assertEqual((visit["cat_id"], visit["box_id"], visit["outcome"], visit["region"]),
                         ("kefir", 1, "urine", 2))
        self.assertGreater(visit["exited_at"], visit["entered_at"])
        self.assertEqual(visit["source"], "mock")
        reopened = Store(self.path, seed=False)
        try:
            self.assertEqual(reopened.snapshot()["visits"][0], visit)
        finally:
            reopened.db.close()
        notices = self.state()["notices"]
        self.assertEqual(len(notices), 2)
        self.assertIn("wyszedł", notices[0]["text"])

    def test_empty_and_uncertain_never_create_a_urine_region(self):
        for scenario in ("empty", "uncertain"):
            visit = self.finished(self.simulate(scenario))
            self.assertEqual(visit["outcome"], scenario)
            self.assertIsNone(visit["region"])
            self.assertIsNone(visit["feces_region"])

    def test_feces_and_mixed_visits_keep_separate_locations(self):
        for scenario, urine_region in (("feces", None), ("both", 2)):
            status, body = self.request("/api/simulate", {"cat_id": "kefir", "box_id": 1,
                "scenario": scenario, "region": 2, "feces_region": 7})
            self.assertEqual(status, 201, body)
            visit_id = json.loads(body)["visit_id"]
            visit = self.finished(visit_id)
            self.assertEqual((visit["outcome"], visit["region"], visit["feces_region"]),
                             (scenario, urine_region, 7))
            self.assertIn("Kał — miejsce: środek z przodu", self.state()["notices"][0]["text"])
            for outcome, expected in (("both_confirmed", (1, 8)), ("feces_confirmed", (None, 8)),
                                      ("confirmed", (1, None)), ("uncertain", (None, None))):
                status, body = self.request(f"/api/visits/{visit_id}/review",
                    {"outcome": outcome, "region": 1, "feces_region": 8})
                self.assertEqual(status, 200, body)
                updated = next(v for v in self.state()["visits"] if v["id"] == visit_id)
                self.assertEqual((updated["region"], updated["feces_region"]), expected)
                self.assertEqual(updated["original_outcome"], scenario)
            exported = self.request("/api/export.csv")[1].decode("utf-8-sig")
            self.assertIn("Miejsce moczu,Miejsce kału", exported)

    def test_invalid_feces_locations_are_rejected(self):
        for region in (None, -1, 9, True, "2"):
            self.assertEqual(self.request("/api/simulate", {"cat_id": "kefir", "box_id": 1,
                "scenario": "both", "region": 2, "feces_region": region})[0], 400)

    def test_legacy_database_migration_preserves_existing_visit(self):
        path = str(Path(self.temp.name) / "legacy.sqlite3")
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE visits (id TEXT PRIMARY KEY, cat_id TEXT, box_id INTEGER, "
                "entered_at TEXT, exited_at TEXT, status TEXT, outcome TEXT, region INTEGER, "
                "scenario TEXT, note TEXT DEFAULT '', original_outcome TEXT, source TEXT DEFAULT 'mock', reviewed_at TEXT)")
            db.execute("INSERT INTO visits (id,cat_id,box_id,status,outcome,region,scenario) "
                       "VALUES ('old','kefir',1,'done','confirmed',2,'urine')")
        for _ in range(2):
            store = Store(path, seed=False)
            try:
                visit = store.snapshot()["visits"][0]
                self.assertEqual((visit["id"], visit["outcome"], visit["region"], visit["feces_region"]),
                                 ("old", "confirmed", 2, None))
            finally:
                store.db.close()

    def test_occupancy_constraints_and_independent_boxes(self):
        self.server.simulation_seconds = 0.7
        first = self.simulate()
        for cat, box in (("kefir", 2), ("kalinka", 1)):
            status, _ = self.request("/api/simulate", {"cat_id": cat, "box_id": box,
                                                      "scenario": "urine", "region": 1})
            self.assertEqual(status, 400)
        second = self.simulate(cat="kalinka", box=2)
        self.assertEqual(self.request("/api/boxes/1/clean", {})[0], 400)
        self.finished(first)
        self.finished(second)

    def test_cleaning_preserves_history_and_review_remains_mock(self):
        visit_id = self.simulate()
        self.finished(visit_id)
        note = '=HYPERLINK("https://example.com")'
        status, body = self.request(f"/api/visits/{visit_id}/review",
                                    {"outcome": "confirmed", "region": 8, "note": note})
        self.assertEqual(status, 200, body)
        visit = self.state()["visits"][0]
        self.assertEqual((visit["outcome"], visit["original_outcome"], visit["source"], visit["region"]),
                         ("confirmed", "urine", "mock", 8))
        self.assertTrue(visit["reviewed_at"])
        self.assertEqual(self.server.store.db.execute("SELECT count(*) FROM reviews").fetchone()[0], 1)
        self.assertEqual(self.request("/api/boxes/1/clean", {})[0], 200)
        snapshot = self.state()
        self.assertEqual(len(snapshot["visits"]), 1)
        self.assertGreater(snapshot["boxes"][0]["cleaned_at"], visit["exited_at"])
        exported = self.request("/api/export.csv")[1].decode("utf-8-sig")
        self.assertIn("SYMULACJA", exported)
        self.assertIn("'=HYPERLINK", exported)
        self.request(f"/api/visits/{visit_id}/review", {"outcome": "empty", "region": 8})
        self.assertIsNone(self.state()["visits"][0]["region"])

    def test_invalid_requests_and_cross_origin_are_rejected(self):
        for body in ([], {"cat_id": "other"}, {"cat_id": "kefir", "box_id": 1,
                                              "scenario": "urine", "region": 9}):
            self.assertEqual(self.request("/api/simulate", body)[0], 400)
        self.assertEqual(self.request("/api/simulate", {}, {"Content-Type":"application/json",
                                                          "Origin":"https://example.com"})[0], 403)
        self.assertEqual(self.request("/api/simulate", {}, {"Content-Type":"text/plain"})[0], 415)
        self.assertEqual(self.request("/../server.py")[0], 404)
        self.assertEqual(self.request("/api/visits/missing/review", {"outcome":"confirmed"})[0], 400)

    def test_precise_waste_points_persist_and_coarse_correction_replaces_them(self):
        visit_id = self.simulate()
        self.finished(visit_id)
        endpoint = f'/api/visits/{visit_id}/review'
        body = {'outcome': 'both_confirmed', 'urine_point': [.27, .48], 'feces_point': [1, 0]}
        self.assertEqual(self.request(endpoint, body)[0], 200)
        visit = self.state()['visits'][0]
        self.assertEqual((visit['region'], visit['urine_point']), (3, [.27, .48]))
        self.assertEqual((visit['feces_region'], visit['feces_point']), (2, [1, 0]))
        reopened = Store(self.path, seed=False)
        try:
            self.assertEqual(reopened.snapshot()['visits'][0]['urine_point'], [.27, .48])
        finally:
            reopened.db.close()
        # Clients retaining the same coarse region must not erase an existing point.
        self.assertEqual(self.request(endpoint, {'outcome': 'confirmed', 'region': 3})[0], 200)
        visit = self.state()['visits'][0]
        self.assertEqual(visit['urine_point'], [.27, .48])
        self.assertIsNone(visit['feces_point'])
        self.assertIsNone(visit['feces_region'])
        self.assertEqual(self.request(endpoint, {'outcome': 'confirmed', 'region': 6})[0], 200)
        self.assertIsNone(self.state()['visits'][0]['urine_point'])
        self.assertEqual(self.request(endpoint, {'outcome': 'empty', 'urine_point': [.27, .48]})[0], 200)
        visit = self.state()['visits'][0]
        self.assertIsNone(visit['region'])
        self.assertIsNone(visit['urine_point'])

    def test_invalid_or_conflicting_precise_points_do_not_change_review(self):
        visit_id = self.simulate()
        self.finished(visit_id)
        endpoint = f'/api/visits/{visit_id}/review'
        before = self.state()['visits'][0]
        for point in ([True, .5], [float('nan'), .5], [0, float('inf')], [-.1, .5],
                      [.5, 1.1], [.5], '.2,.3', {'x': .2, 'y': .3}):
            for field in ('urine_point', 'feces_point'):
                with self.subTest(point=point, field=field):
                    self.assertEqual(self.request(endpoint, {'outcome': 'both', field: point})[0], 400)
        self.assertEqual(self.request(endpoint, {'outcome': 'urine', 'region': 8,
                                                'urine_point': [.27, .48]})[0], 400)
        self.assertEqual(self.state()['visits'][0], before)
        self.assertEqual(self.server.store.db.execute('SELECT count(*) FROM reviews').fetchone()[0], 0)

    def test_sse_emits_initial_snapshot_and_visit_update(self):
        with urlopen(self.base + "/api/events", timeout=3) as response:
            self.assertEqual(response.readline(), b"event: state\n")
            initial = json.loads(response.readline().decode().removeprefix("data: "))
            self.assertEqual(initial["visits"], [])
            self.assertEqual(response.readline(), b"\n")
            visit_id = self.simulate()
            self.assertEqual(response.readline(), b"event: state\n")
            updated = json.loads(response.readline().decode().removeprefix("data: "))
            self.assertEqual(updated["visits"][0]["id"], visit_id)
            self.assertEqual(updated["visits"][0]["status"], "active")
        self.finished(visit_id)

    def test_restart_marks_interrupted_visit_uncertain(self):
        path = str(Path(self.temp.name) / "restart.sqlite3")
        store = Store(path, seed=False)
        visit_id = store.start({"cat_id":"kefir", "box_id":1, "scenario":"urine", "region":2})
        store.db.close()
        store = Store(path, seed=False)
        try:
            visit = store.snapshot()["visits"][0]
            self.assertEqual(visit["id"], visit_id)
            self.assertEqual((visit["status"], visit["outcome"], visit["region"]), ("done", "uncertain", None))
            self.assertIn("restart", visit["note"])
        finally:
            store.db.close()

    def test_local_assets_serve_successfully(self):
        for path in ("/", "/app.js", "/style.css", "/favicon.svg"):
            status, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(body)


class QwenAdapterTests(unittest.TestCase):
    def test_connection_and_service_errors_are_distinct_from_invalid_requests(self):
        errors = [URLError('connection refused'), TimeoutError(), ConnectionResetError(),
                  *[HTTPError('http://model', code, 'failure', {}, None) for code in (401, 403, 404, 408, 429, 500, 503)]]
        with patch.dict(os.environ, {'QWEN_BASE_URL': 'http://localhost:1234/v1'}):
            for error in errors:
                with self.subTest(error=type(error).__name__, code=getattr(error, 'code', None)), \
                     patch('qwen.urlopen', side_effect=error):
                    with self.assertRaises(ModelUnavailable):
                        analyze_images([('image/png', b'image')], 'test')
            with patch('qwen.urlopen', side_effect=HTTPError('http://model', 400, 'bad input', {}, None)):
                with self.assertRaises(HTTPError):
                    analyze_images([('image/png', b'image')], 'test')

    def test_adapter_sends_images_to_configured_model_and_returns_observations(self):
        reply = io.BytesIO(json.dumps({"choices":[{"message":{"content":"Widoczna ciemna plama."}}]}).encode())
        with patch.dict(os.environ, {"QWEN_BASE_URL":"http://localhost:1234/v1", "QWEN_MODEL":"existing-qwen"}), \
             patch("qwen.urlopen", return_value=reply) as request:
            result = analyze_images([("image/png", b"test-image")], "Kuweta 1")
        self.assertEqual(result, "Widoczna ciemna plama.")
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "http://localhost:1234/v1/chat/completions")
        payload = json.loads(sent.data)
        self.assertEqual(payload["model"], "existing-qwen")
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertTrue(payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_adapter_rejects_reasoning_without_image_observations(self):
        for content in (None, "", "   "):
            with self.subTest(content=content):
                reply = io.BytesIO(json.dumps({"choices": [{"finish_reason": "length", "message": {
                    "content": content, "reasoning_content": "Unfinished reasoning"}}]}).encode())
                with patch.dict(os.environ, {"QWEN_BASE_URL": "http://localhost:1234/v1"}), \
                     patch("qwen.urlopen", return_value=reply):
                    with self.assertRaisesRegex(ValueError, "nie zwrócił opisu"):
                        analyze_images([("image/png", b"test-image")], "Kuweta 1")

    def test_identity_labels_stay_next_to_corresponding_images(self):
        reply = io.BytesIO(json.dumps({'choices': [{'message': {'content': '{"cat_id":"unknown"}'}}]}).encode())
        with patch.dict(os.environ, {'QWEN_BASE_URL': 'http://localhost:1234/v1'}), \
             patch('qwen.urlopen', return_value=reply) as request:
            analyze_images([('image/jpeg', b'reference'), ('image/jpeg', b'target')], 'Porównaj kota',
                           task='identity', image_labels=['Wzorzec: Kalinka', 'Obraz do rozpoznania'])
        content = json.loads(request.call_args.args[0].data)['messages'][0]['content']
        self.assertEqual(content[1]['text'], 'Wzorzec: Kalinka')
        self.assertEqual(content[2]['type'], 'image_url')
        self.assertEqual(content[3]['text'], 'Obraz do rozpoznania')
        self.assertEqual(content[4]['type'], 'image_url')


if __name__ == "__main__":
    unittest.main()
