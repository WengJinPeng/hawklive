import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from windows_power import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, SystemSleepInhibitor, execution_state_api


class SleepPreventionTests(unittest.TestCase):
    def test_non_windows_does_not_load_windows_libraries(self):
        with patch('windows_power.os', SimpleNamespace(name='posix')), patch('windows_power.execution_state_api') as api:
            with SystemSleepInhibitor() as guard:
                self.assertEqual(guard.status(), {'state': 'unsupported', 'system_required': False})
            api.assert_not_called()

    def test_active_request_preserves_display_timeout_and_restores_previous_state(self):
        # A caller's pre-existing display request must survive our cleanup.
        api = Mock(side_effect=[ES_CONTINUOUS | 2, ES_CONTINUOUS | ES_SYSTEM_REQUIRED])
        with patch('windows_power.os', SimpleNamespace(name='nt')), patch('windows_power.execution_state_api', return_value=api):
            with SystemSleepInhibitor() as guard:
                self.assertTrue(guard.status()['system_required'])
            self.assertEqual(guard.state, 'inactive')
        self.assertEqual([c.args[0] for c in api.call_args_list], [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS | 2])

    def test_rejected_request_is_visible_and_does_not_interrupt_collection(self):
        api = Mock(return_value=0)
        with patch('windows_power.os', SimpleNamespace(name='nt')), patch('windows_power.execution_state_api', return_value=api), self.assertLogs('windows_power', 'WARNING'):
            with SystemSleepInhibitor() as guard:
                self.assertEqual(guard.status(), {'state': 'failed', 'system_required': False})
            api.assert_called_once()

    def test_missing_api_is_nonfatal(self):
        with patch('windows_power.os', SimpleNamespace(name='nt')), patch('windows_power.execution_state_api', side_effect=OSError('unavailable')), self.assertLogs('windows_power', 'WARNING'):
            with SystemSleepInhibitor() as guard:
                self.assertEqual(guard.state, 'failed')

    def test_cleanup_failure_does_not_mask_original_error(self):
        api = Mock(side_effect=[ES_CONTINUOUS, 0])
        with patch('windows_power.os', SimpleNamespace(name='nt')), patch('windows_power.execution_state_api', return_value=api), self.assertLogs('windows_power', 'WARNING'):
            with self.assertRaisesRegex(ValueError, 'collector failed'):
                with SystemSleepInhibitor() as guard:
                    raise ValueError('collector failed')
            self.assertEqual(guard.state, 'failed')

    def test_collector_lifetime_releases_request_even_on_startup_failure(self):
        import dashboard_server
        api = Mock(side_effect=[ES_CONTINUOUS, ES_CONTINUOUS | ES_SYSTEM_REQUIRED])
        def fail_start(bind, port):
            self.assertTrue(dashboard_server.POWER_GUARD.status()['system_required'])
            raise OSError('port in use')
        with patch('windows_power.os', SimpleNamespace(name='nt')), patch('windows_power.execution_state_api', return_value=api), patch('dashboard_server._run_collector', side_effect=fail_start):
            with self.assertRaisesRegex(OSError, 'port in use'):
                dashboard_server.run_collector()
        self.assertIsNone(dashboard_server.POWER_GUARD)
        self.assertEqual(api.call_args_list[-1].args[0], ES_CONTINUOUS)

    @unittest.skipUnless(os.name == 'nt', 'Windows native execution-state check')
    def test_native_request_is_present_then_cleared_on_same_thread(self):
        api = execution_state_api()
        original = api(ES_CONTINUOUS)
        self.assertNotEqual(original, 0)
        try:
            with SystemSleepInhibitor() as guard:
                self.assertEqual(guard.state, 'active')
                previous = api(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                self.assertTrue(previous & ES_SYSTEM_REQUIRED)
            after = api(ES_CONTINUOUS)
            self.assertNotEqual(after, 0)
            self.assertFalse(after & ES_SYSTEM_REQUIRED)
        finally:
            api(original | ES_CONTINUOUS)


if __name__ == '__main__':
    unittest.main()
