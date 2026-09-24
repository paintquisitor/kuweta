import tempfile
import unittest
from pathlib import Path

from server import Store


class RecordingIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "recordings.sqlite3"
        self.store = Store(self.path, seed=False)
        self.start = 1790184058

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def result(self, end=None):
        return {"camera_host": "192.0.2.1", "recordings": [{
            "startTime": self.start, "endTime": end or self.start + 66, "vedio_type": 2}],
            "checked_through": self.start + 200, "clock_offset_seconds": 0}

    def test_repeated_poll_and_restart_preserve_camera_times_without_creating_visits(self):
        self.store.recordings.ingest(self.result(), self.start + 200)
        self.store.db.close()
        self.store = Store(self.path, seed=False)
        self.store.recordings.ingest(self.result(), self.start + 500)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["visits"], [])
        self.assertEqual(len(snapshot["notices"]), 1)
        records = snapshot["recordings"]["items"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["started_at"], "2026-09-23T17:20:58+00:00")
        self.assertEqual(records[0]["ended_at"], "2026-09-23T17:22:04+00:00")
        self.assertNotEqual(records[0]["detected_at"], records[0]["started_at"])

    def test_growing_recording_waits_for_end_and_notifies_once(self):
        self.store.recordings.ingest(self.result(self.start + 10), self.start + 20)
        self.assertEqual(self.store.snapshot()["notices"], [])
        self.assertEqual(self.store.recordings.snapshot()["items"][0]["status"], "recording")
        self.store.recordings.ingest(self.result(self.start + 70), self.start + 100)
        self.assertEqual(self.store.snapshot()["notices"], [])
        self.store.recordings.ingest(self.result(self.start + 70), self.start + 140)
        self.store.recordings.ingest(self.result(self.start + 65), self.start + 150)
        row = self.store.recordings.snapshot()["items"][0]
        self.assertEqual(row["end_epoch"], self.start + 70)
        self.assertEqual(row["status"], "pending_analysis")
        self.assertEqual(len(self.store.snapshot()["notices"]), 1)

    def test_ignored_human_test_stays_ignored_and_failed_poll_does_not_advance_cursor(self):
        self.store.recordings.ingest(self.result(), self.start + 80)
        with self.store.db:
            self.store.db.execute("UPDATE camera_recordings SET status='ignored_test'")
        self.store.recordings.ingest(self.result(), self.start + 200)
        self.store.recordings.failed("Kamera offline")
        status = self.store.recordings.snapshot()
        self.assertEqual(status["items"][0]["status"], "ignored_test")
        self.assertEqual(status["cursor_epoch"], self.start + 200)
        self.assertEqual(self.store.snapshot()["notices"], [])
        invalid = self.result()
        invalid["recordings"].append({"startTime": self.start + 300, "endTime": self.start})
        with self.assertRaises(ValueError):
            self.store.recordings.ingest(invalid, self.start + 1000)
        self.assertEqual(self.store.recordings.snapshot()["last_checked_at"], status["last_checked_at"])
        self.assertEqual(len(self.store.recordings.snapshot()["items"]), 1)


if __name__ == "__main__":
    unittest.main()
