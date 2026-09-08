from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from storage_maintenance import LocalStorageManager


class LocalStorageManagerTests(unittest.TestCase):
    @patch("storage_maintenance.shutil.disk_usage")
    def test_large_absolute_free_space_does_not_warn_on_percentage_alone(self, disk_usage) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "data.sqlite3"
            database.touch()
            disk_usage.return_value = SimpleNamespace(
                total=1024 * 1024**3, used=956 * 1024**3, free=68 * 1024**3
            )
            manager = LocalStorageManager(database, root / "backups")
            manager._integrity = "ok"
            self.assertEqual(manager.status()["state"], "ok")

    def test_backup_and_retention_never_delete_unsynced_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "data.sqlite3"
            old = time.time() - 100 * 86400
            with sqlite3.connect(database) as db:
                db.execute(
                    "CREATE TABLE readings(id INTEGER PRIMARY KEY, timestamp REAL, synced_at REAL)"
                )
                db.executemany(
                    "INSERT INTO readings(timestamp,synced_at) VALUES(?,?)",
                    [(old, old + 1), (old, None), (time.time(), time.time())],
                )
                db.execute(
                    "CREATE TABLE logs(id INTEGER PRIMARY KEY, timestamp REAL, message TEXT)"
                )
                db.execute("INSERT INTO logs(timestamp,message) VALUES(?,?)", (old, "old"))

            manager = LocalStorageManager(
                database, root / "backups", retention_days=90, interval_seconds=60
            )
            manager.run_once()

            with sqlite3.connect(database) as db:
                rows = db.execute(
                    "SELECT timestamp,synced_at FROM readings ORDER BY id"
                ).fetchall()
                log_count = db.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
            self.assertEqual(len(rows), 2)
            self.assertIsNone(rows[0][1])
            self.assertEqual(log_count, 0)
            self.assertEqual(len(list((root / "backups").glob("local-*.sqlite3"))), 1)
            self.assertEqual(manager.status()["integrity"], "ok")
            self.assertTrue(manager.status()["unsynced_records_protected"])

    def test_old_managed_backups_are_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "data.sqlite3"
            with sqlite3.connect(database):
                pass
            backup_dir = root / "backups"
            backup_dir.mkdir()
            old_backup = backup_dir / "local-2020-01-01.sqlite3"
            old_backup.write_bytes(b"managed")
            old_time = time.time() - 20 * 86400
            os.utime(old_backup, (old_time, old_time))

            manager = LocalStorageManager(database, backup_dir, backup_retention_days=7)
            manager.run_once()
            self.assertFalse(old_backup.exists())


if __name__ == "__main__":
    unittest.main()
