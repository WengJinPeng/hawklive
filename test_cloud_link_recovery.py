from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import time
import unittest
import subprocess
import shutil
from unittest.mock import MagicMock, patch

from cloud_api import AlarmBatch, AlarmEventIn, ingest_alarms, reading_dict
from cloud_sync import CloudSyncService


class CloudLinkRecoveryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required')
    def test_rendered_connection_state_runtime(self):
        result = subprocess.run(['node', '--test', str(Path(__file__).with_name('test_connection_state_runtime.js'))], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @patch('cloud_api.authorize_ingest_target')
    @patch('cloud_api.connect')
    def test_open_alarm_uses_typed_nullable_timestamp(self, connect, _authorize):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None
        connect.return_value.__enter__.return_value = db
        event = AlarmEventIn(event_uuid='11111111-1111-4111-8111-111111111111', customer_id='c', site_id='s',
                             cleanroom_id='r', device_id='d', source='device', metric='temperature',
                             started_at=100, ended_at=None, limit_description='test')
        ingest_alarms(AlarmBatch(site_id='s', events=[event]), {'customer_id': 'c', 'site_id': 's'})
        query, args = next(call.args for call in db.execute.call_args_list
                           if 'INSERT INTO alarm_events(' in call.args[0])
        self.assertIn('to_timestamp(%s::double precision)', query)
        self.assertNotIn('CASE WHEN %s IS NULL', query)
        self.assertEqual(len(args), query.count('%s'))
        self.assertIsNone(args[8])

    def test_stale_reading_does_not_assert_physical_disconnect(self):
        row = [None, 'r', 'room', 'd', 'device', datetime.fromtimestamp(time.time()-120, timezone.utc),
               'device', {}, {}, 'NORMAL', [], None, None, 1, 'PCS/28.3L', 'profile']
        self.assertEqual(reading_dict(tuple(row))['connection_state'], 'sync_stale')
        row[5] = datetime.now(timezone.utc)
        self.assertEqual(reading_dict(tuple(row))['connection_state'], 'online')

    def test_blocked_alarm_cannot_block_latest_or_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            service = CloudSyncService(Path(directory)/'db.sqlite3', 'https://example.invalid', 'token', 'site', lambda *_: None)
            blocked = threading.Event()
            release = threading.Event()
            seen_latest = threading.Event()
            seen_heartbeat = threading.Event()
            def alarm():
                blocked.set()
                release.wait(3)
                raise RuntimeError('HTTP 500')
            service.pull_config_once = lambda: 0
            service.sync_alarms_once = alarm
            service.sync_once = lambda: 0
            service.sync_latest_once = lambda: seen_latest.set() or 0
            service.heartbeat_once = lambda: seen_heartbeat.set() or 0
            service.start()
            try:
                self.assertTrue(blocked.wait(1))
                self.assertTrue(seen_latest.wait(1))
                self.assertTrue(seen_heartbeat.wait(1))
                self.assertFalse(release.is_set())
            finally:
                release.set()
                service.stop()
            self.assertFalse(service._thread.is_alive())

    def test_other_channel_success_does_not_hide_alarm_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            service = CloudSyncService(Path(directory)/'db.sqlite3', 'https://example.invalid', 'token', 'site', lambda *_: None)
            service._channel_context.name = 'alarms'
            service._mark_failure(RuntimeError('HTTP 500'))
            service._channel_context.name = 'latest'
            service._mark_success()
            self.assertIn('alarms', service.last_error)
            service._channel_context.name = 'alarms'
            service._mark_success()
            self.assertIsNone(service.last_error)
