import unittest
from connection_status import device_snapshot, cloud_device_status
from cloud_api import CollectorHeartbeat
from pydantic import ValidationError


class ConnectionStatusTests(unittest.TestCase):
    def snapshot(self, latest, running=True, poll=10):
        return device_snapshot([{'id': 'a'}, {'id': 'b'}], latest, running=running, poll_seconds=poll, now=1000)

    def test_success_failure_and_no_check_are_different(self):
        result = self.snapshot([{'device_id': 'a', 'source': 'device', 'online': True, 'timestamp': 990},
                                {'device_id': 'b', 'online': False, 'error': 'Socket closed', 'timestamp': 995}])
        self.assertEqual((result['device_online'], result['device_offline'], result['device_unknown']), (1, 1, 0))
        self.assertEqual(self.snapshot([])['device_unknown'], 2)

    def test_stopped_stale_demo_and_unconfigured_readings_never_count_online(self):
        for item, running in [({'timestamp': 990, 'source': 'device'}, False),
                              ({'timestamp': 800, 'source': 'device'}, True),
                              ({'timestamp': 990, 'source': 'demo'}, True)]:
            result = self.snapshot([{'device_id': 'a', 'online': True, **item},
                                    {'device_id': 'deleted', 'online': True, 'source': 'device', 'timestamp': 990}], running)
            self.assertEqual(result['device_online'], 0)
            self.assertEqual(result['device_unknown'], 2)

    def test_poll_interval_and_failure_backoff_keep_valid_checks(self):
        self.assertEqual(self.snapshot([{'device_id':'a','source':'device','online':True,'timestamp':500}],poll=600)['device_online'],1)
        self.assertEqual(self.snapshot([{'device_id':'a','online':False,'error':'closed','timestamp':800,'retry_at':1100}])['device_offline'],1)

    def test_collector_disconnect_or_stopped_monitor_invalidates_cached_device_states(self):
        snapshot = self.snapshot([{'device_id':'a','source':'device','online':True,'timestamp':990}])
        for connected, running in [(False,True),(True,False)]:
            result = cloud_device_status({**snapshot,'monitor_running':running},connected)
            self.assertEqual(result['device_online'],0)
            self.assertEqual(result['device_unknown'],2)
            self.assertEqual(result['last_reported_device_online'],1)

    def test_legacy_zero_reads_do_not_assert_failed_checks(self):
        result = cloud_device_status({'device_total':2,'device_online':0,'monitor_running':True},True)
        self.assertEqual(result['device_offline'],0)
        self.assertEqual(result['device_unknown'],2)

    def test_heartbeat_rejects_duplicates_and_inconsistent_counts(self):
        data = dict(site_id='site',collector_time=1000,version='test',hostname='qa',platform='qa',monitor_running=True,device_total=2,device_online=1)
        CollectorHeartbeat(**data)  # Existing collectors remain compatible.
        for states in [[{'device_id':'a','state':'online'}]*2,
                       [{'device_id':'a','state':'offline'},{'device_id':'b','state':'unknown'}]]:
            with self.assertRaises(ValidationError): CollectorHeartbeat(**data,device_states=states)
