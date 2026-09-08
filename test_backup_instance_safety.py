from contextlib import closing
from pathlib import Path
from unittest.mock import patch, MagicMock
import os
import sqlite3
import tempfile
import unittest

from storage_maintenance import LocalStorageManager
from single_instance import SingleInstanceLock, AlreadyRunningError
import dashboard_server
import windows_launcher


class BackupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / 'data.sqlite3'
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE proof(value TEXT)')
            db.execute("INSERT INTO proof VALUES ('preserved')")
        self.manager = LocalStorageManager(self.database, self.root/'backups')

    def test_connections_are_closed_before_atomic_replace(self):
        original_connect, original_replace = sqlite3.connect, os.replace
        opened = []
        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            opened.append(connection)  # Keep alive so GC cannot mask leaks.
            return connection
        def replace(source, target):
            self.assertGreaterEqual(len(opened), 2)
            for connection in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute('SELECT 1')
            original_replace(source, target)
        with patch('storage_maintenance.sqlite3.connect', side_effect=connect), patch('storage_maintenance.os.replace', side_effect=replace):
            self.manager.run_once()
        self.assertIsNone(self.manager.status()['last_error'])
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')
        backup_path = next((self.root/'backups').glob('local-*.sqlite3'))
        with closing(original_connect(backup_path)) as backup:
            self.assertEqual(backup.execute('SELECT value FROM proof').fetchone()[0], 'preserved')

    def test_foreign_temporary_file_is_not_removed(self):
        self.manager.backup_dir.mkdir()
        old = self.manager._backup_path(0).with_suffix('.sqlite3.tmp')
        old.write_bytes(b'owned by another attempt')
        self.manager.run_once(now=0)
        self.assertEqual(old.read_bytes(), b'owned by another attempt')
        self.assertIsNone(self.manager.status()['last_error'])

    def test_failed_replace_cleans_only_own_temp_and_recovers(self):
        with patch('storage_maintenance.os.replace', side_effect=PermissionError('busy')):
            self.manager.run_once()
        self.assertEqual(self.manager.status()['last_error'], 'busy')
        self.assertFalse(list(self.manager.backup_dir.glob('*.tmp')))
        self.manager.run_once()
        self.assertIsNone(self.manager.status()['last_error'])

    def test_concurrent_maintenance_skips_then_recovers(self):
        lock = SingleInstanceLock(self.manager.backup_dir/'maintenance.lock')
        lock.acquire()
        try:
            with patch.object(self.manager, '_create_daily_backup') as backup:
                self.manager.run_once()
                backup.assert_not_called()
        finally:
            lock.release()
        self.manager.run_once()
        self.assertEqual(self.manager.status()['integrity'], 'ok')

    def test_inaccessible_lock_is_reported_without_stopping_collector(self):
        with patch.object(SingleInstanceLock, 'acquire', side_effect=PermissionError('denied')):
            self.manager.run_once()
        self.assertEqual(self.manager.status()['last_error'], 'denied')


class StartupSafetyTests(unittest.TestCase):
    def test_machine_gate_precedes_database_and_migration(self):
        with patch('dashboard_server.MachineInstanceLock') as lock, patch('dashboard_server.init_db') as initialize, patch('dashboard_server.migrate_legacy_runtime_data') as migrate:
            lock.return_value.acquire.side_effect = AlreadyRunningError('running')
            with self.assertRaises(AlreadyRunningError):
                dashboard_server.run_collector()
            initialize.assert_not_called()
            migrate.assert_not_called()

    def test_portable_duplicate_has_clean_exit_code(self):
        with tempfile.TemporaryDirectory() as root, patch('windows_launcher.configure_environment', return_value=Path(root)), patch('dashboard_server.run_collector', side_effect=AlreadyRunningError('running')):
            self.assertEqual(windows_launcher.main(['--device', '--no-browser']), 2)


if __name__ == '__main__':
    unittest.main()
