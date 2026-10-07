import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from collector_startup import fresh_startup, record_startup, registration_failure, startup_message


class StartupTests(unittest.TestCase):
    def test_old_failure_is_not_attributed_to_new_install(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch('collector_startup.time.time', return_value=10):
                record_startup(root, 'network_failed')
            self.assertEqual(fresh_startup(root, 20), {})
            self.assertIn('原因未确认', startup_message(root, 20))

    def test_disk_warning_does_not_claim_root_cause(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('collector_startup.shutil.disk_usage', return_value=SimpleNamespace(free=100*1024*1024)):
                message = startup_message(Path(folder), 0)
            self.assertIn('100 MB', message)
            self.assertIn('可能影响', message)
            self.assertIn('不要删除', message)

    def test_error_classification_never_copies_tokens(self):
        self.assertEqual(registration_failure(urllib.error.HTTPError('https://secret/?token=abc', 403, 'secret', {}, None)), 'registration_rejected')
        self.assertEqual(registration_failure(urllib.error.URLError(ssl.SSLError('secret'))), 'tls_failed')
        self.assertEqual(registration_failure(urllib.error.URLError(OSError('secret'))), 'network_failed')
        self.assertEqual(registration_failure(ValueError('secret')), 'registration_failed')

    def test_pending_and_network_have_operator_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            record_startup(root, 'pending')
            self.assertIn('云端批准', startup_message(root, 0))
            record_startup(root, 'network_failed')
            self.assertIn('恢复连接后会自动重试', startup_message(root, 0))

    def test_full_disk_does_not_break_worker(self):
        with patch('collector_startup.atomic_json', side_effect=OSError('disk full')):
            record_startup(Path('unused'), 'starting')

    def test_unknown_code_cannot_leak_arbitrary_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            record_startup(root, 'token=secret')
            message = startup_message(root, 0)
            self.assertNotIn('secret', message)
            self.assertIn('startup_unconfirmed', message)
