from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import maintenance_index as m
from collector_maintenance import CollectorMaintenance
from test_collector_maintenance import create_local,reading


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.path=self.root/'qa.sqlite3'
        create_local(self.path,[reading() for _ in range(103)]+[reading(customer_id='other')])
        self.service=SimpleNamespace(db_path=self.path,customer_id='customer-a',site_id='site-a',token='test')
    def tearDown(self):self.temp.cleanup()
    def digest(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            return hashlib.sha256(repr(db.execute('SELECT * FROM readings ORDER BY id').fetchall()).encode()).hexdigest()
    def test_count_pagination_readonly_and_other_customer_preserved(self):
        before=self.digest();old=CollectorMaintenance(self.service)
        pages=[old.snapshot() for _ in range(3)]
        self.assertTrue(m.ensure(self.path))
        new=CollectorMaintenance(self.service)
        self.assertEqual([new.snapshot() for _ in range(3)],pages)
        self.assertEqual(self.digest(),before)
        with closing(sqlite3.connect(self.path)) as db, db:
            for select in ('COUNT(*)','record_uuid'):
                plan=str(db.execute(f"EXPLAIN QUERY PLAN SELECT {select} FROM {m.table(db)} WHERE customer_id=? AND site_id=? AND source='device' AND sync_quarantined_at IS NOT NULL",('customer-a','site-a')).fetchall())
                self.assertIn(m.NAME,plan)
            # A non-null zero or empty string is quarantined by the old contract too.
            db.execute("UPDATE readings SET sync_quarantined_at=0 WHERE id=1")
            db.execute("UPDATE readings SET sync_quarantined_at='' WHERE id=2")
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM readings INDEXED BY {m.NAME} WHERE source='device' AND sync_quarantined_at IS NOT NULL").fetchone()[0],104)
    def test_interrupted_migration_rolls_back_and_preserves_all_rows(self):
        before=self.digest();self.assertFalse(m.ensure(self.path,budget=0))
        with closing(sqlite3.connect(self.path)) as db, db:self.assertFalse(m.usable(db))
        self.assertEqual(self.digest(),before)
        self.assertFalse(m.ensure(self.path)) # backoff survives new instance
        self.assertEqual(json.loads((self.root/'maintenance-index-state.json').read_text())['attempts'],1)
    def test_wrong_definition_uses_original_query_and_does_not_replace_index(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(f'CREATE INDEX {m.NAME} ON readings(timestamp)')
            self.assertEqual(m.table(db),'readings')
        self.assertFalse(m.ensure(self.path))
        self.assertEqual(CollectorMaintenance(self.service).snapshot()['total'],103)
    def test_no_default_activation_wrong_tenant_and_trial_are_blocked(self):
        self.assertFalse(m.enabled(self.service))
        policy=dict(enabled=True,expires_at=time.time()+60,scope=dict(customer_id='customer-a',site_id='site-a'))
        (self.root/'maintenance-index-policy.json').write_text(json.dumps(policy))
        self.assertTrue(m.enabled(self.service))
        with patch.dict('os.environ',{'DCP_UPDATE_TRIAL':'1'}):self.assertFalse(m.enabled(self.service))
        self.service.customer_id='other';self.assertFalse(m.enabled(self.service))
    def test_writer_contention_defers_without_dropping_or_modifying_data(self):
        before=self.digest()
        with closing(sqlite3.connect(self.path)) as writer, writer:
            writer.execute('BEGIN IMMEDIATE')
            start=time.monotonic();self.assertFalse(m.ensure(self.path))
            self.assertLess(time.monotonic()-start,.5)
        self.assertEqual(self.digest(),before)

    def test_prepare_failure_falls_back_with_original_pagination(self):
        collector=CollectorMaintenance(self.service)
        with patch.object(m,'table',return_value='readings INDEXED BY '+m.NAME):
            self.assertEqual(collector.snapshot()['total'],103)
