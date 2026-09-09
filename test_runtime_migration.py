import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from collector_activation import INSTALLATION_ID_FILENAME, load_or_create_installation_id
from runtime_paths import migrate_legacy_runtime_data, runtime_paths


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.old = root / "old"
        self.old.mkdir()
        self.paths = runtime_paths(environ={"DCP_DATA_DIR": str(root / "new")})
        self.settings = self.old / "collector_settings.json"
        self.settings.write_text(json.dumps({
            "cloud_url": "https://qa.invalid", "site_id": "qa-legacy",
            "token": "local-qa-only-token-1234567890",
        }), encoding="utf-8")
        self.identity = load_or_create_installation_id(self.settings)

    def migrate(self):
        return migrate_legacy_runtime_data(self.paths, app_directory=self.old)

    def test_committed_wal_pending_records_and_identity_survive_together(self):
        with closing(sqlite3.connect(self.old / "dashboard_data.sqlite3")) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE outbox(uuid TEXT PRIMARY KEY, pending INTEGER)")
            db.execute("INSERT INTO outbox VALUES ('qa-pending-record',1)")
            db.commit()
            self.migrate()
            with closing(sqlite3.connect(self.paths.database)) as migrated:
                self.assertEqual(migrated.execute("SELECT * FROM outbox").fetchall(),
                                 [("qa-pending-record", 1)])
        self.assertEqual(load_or_create_installation_id(self.paths.collector_settings), self.identity)
        self.assertEqual(self.paths.collector_settings.read_bytes(), self.settings.read_bytes())
        self.assertEqual(self.migrate(), [])
        self.assertEqual(load_or_create_installation_id(self.settings), self.identity)

    def test_conflicting_identity_fails_before_copying_database_or_settings(self):
        destination_id = load_or_create_installation_id(self.paths.collector_settings)
        with self.assertRaisesRegex(RuntimeError, "conflicts"):
            self.migrate()
        self.assertFalse(self.paths.database.exists())
        self.assertFalse(self.paths.collector_settings.exists())
        self.assertEqual(load_or_create_installation_id(self.paths.collector_settings), destination_id)

    def test_existing_destination_settings_do_not_receive_other_collectors_database(self):
        self.paths.ensure()
        self.paths.collector_settings.write_text('{"site_id":"another-collector"}', encoding="utf-8")
        (self.old / "dashboard_data.sqlite3").write_bytes(b"must not be imported")
        self.assertEqual(self.migrate(), [])
        self.assertFalse(self.paths.database.exists())
        self.assertFalse((self.paths.data_dir / INSTALLATION_ID_FILENAME).exists())

    def test_invalid_source_identity_fails_without_creating_destination(self):
        (self.old / INSTALLATION_ID_FILENAME).write_text("broken", encoding="ascii")
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            self.migrate()
        self.assertFalse(self.paths.data_dir.exists())

    def test_legacy_settings_without_identity_require_fresh_destination(self):
        (self.old / INSTALLATION_ID_FILENAME).unlink()
        self.migrate()
        self.assertEqual(self.paths.collector_settings.read_bytes(), self.settings.read_bytes())
        self.paths.collector_settings.unlink()
        load_or_create_installation_id(self.paths.collector_settings)
        with self.assertRaisesRegex(RuntimeError, "no matching"):
            self.migrate()

    def test_interrupted_settings_publish_retries_with_same_migrated_identity(self):
        replace = os.replace
        def interrupted(source, destination):
            if Path(destination) == self.paths.collector_settings:
                raise OSError("injected interruption")
            return replace(source, destination)
        with patch("runtime_paths.os.replace", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "injected"):
                self.migrate()
        self.assertFalse(self.paths.collector_settings.exists())
        self.assertEqual(load_or_create_installation_id(self.paths.collector_settings), self.identity)
        self.migrate()
        self.assertEqual(load_or_create_installation_id(self.paths.collector_settings), self.identity)
        self.assertEqual(self.paths.collector_settings.read_bytes(), self.settings.read_bytes())
        self.assertEqual(list(self.paths.data_dir.glob("collector-migration-*")), [])


if __name__ == "__main__":
    unittest.main()
