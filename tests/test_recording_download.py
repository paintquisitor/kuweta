import unittest
from unittest.mock import Mock
from recording_download import require_recording, RecordingUnavailable


class ArchiveGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_exact_interval_and_reads_later_pages(self):
        page = [{'record': {'startTime': i, 'endTime': i + 1}} for i in range(100)]
        tapo = Mock()
        tapo.getRecordings.side_effect = [page, [{'record': {'startTime': 100, 'endTime': 280}}]]
        await require_recording(tapo, '20260924', 100, 280)
        self.assertEqual(tapo.getRecordings.call_args.kwargs, dict(start_index=100, end_index=199))
        for entries in ([], [{'record': {'startTime': 300, 'endTime': 480}}],
                        [{'record': {'startTime': 100, 'endTime': 200}}]):
            tapo.getRecordings.side_effect = None
            tapo.getRecordings.return_value = entries
            with self.assertRaises(RecordingUnavailable):
                await require_recording(tapo, '20260924', 100, 280)

    async def test_camera_failure_is_not_reported_as_deleted_recording(self):
        tapo = Mock()
        tapo.getRecordings.side_effect = ConnectionError()
        with self.assertRaises(ConnectionError):
            await require_recording(tapo, '20260924', 100, 280)
