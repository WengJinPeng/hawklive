import json
import os
import tempfile
import unittest
from pathlib import Path
from scripts.cleanup_collector_packages import cleanup


class PackageCleanupTests(unittest.TestCase):
    def test_preserves_current_rollback_recent_and_unrelated_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index in range(6):
                p = root / (str(index)*64 + '.exe')
                p.write_bytes(b'MZ')
                os.utime(p, (index*1000, index*1000))
            current = '0'*64
            (root/'stable.json').write_text(json.dumps({'release': {'sha256': current}}))
            (root/'customer.sqlite3').write_bytes(b'data')
            recent = root/('a'*64 + '.exe'); recent.write_bytes(b'MZ')
            os.utime(recent, (999999, 999999))
            removed = cleanup(root, now=1000000)
            self.assertEqual(set(removed), {str(i)*64+'.exe' for i in (1, 2, 3, 4)})
            self.assertTrue((root/(current+'.exe')).exists())
            self.assertTrue((root/('5'*64+'.exe')).exists())
            self.assertTrue(recent.exists())
            self.assertEqual((root/'customer.sqlite3').read_bytes(), b'data')

    def test_missing_channel_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); p=root/('a'*64+'.exe'); p.write_bytes(b'MZ')
            with self.assertRaises(FileNotFoundError): cleanup(root)
            self.assertTrue(p.exists())
