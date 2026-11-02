from __future__ import annotations

import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cloud_sync import CloudSyncService
from collector_maintenance import CollectorMaintenance
import maintenance_protocol as protocol


def reading(record_uuid=None, **values):
    row = {'record_uuid':record_uuid or uuid4().hex,'customer_id':'customer-a','site_id':'site-a',
           'cleanroom_id':'room-a','cleanroom':'Historic room','device_id':'device-a','device':'Historic sensor',
           'timestamp':1000.123456,'source':'device','particles_json':'{"pm_0_5_um":123}',
           'environment_json':'{"temperature":24,"humidity":50}','alarm_status':'NORMAL','alarm_details_json':'[]',
           'cleanliness_code':1,'cleanliness_label':'ISO','particle_unit_code':1,
           'particle_unit_label':'PCS/28.3L','protocol_profile':'fixture-profile'}
    row.update(values)
    return row


def create_local(path, rows):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('PRAGMA journal_mode=WAL')
        types={'timestamp':'REAL','cleanliness_code':'INTEGER','particle_unit_code':'INTEGER'}
        fields = ','.join('"'+field+'" '+types.get(field,'TEXT') for field in protocol.SOURCE_FIELDS)
        db.execute('CREATE TABLE readings(id INTEGER PRIMARY KEY,'+fields+',sync_attempts INTEGER DEFAULT 5,sync_error TEXT,sync_quarantined_at REAL,synced_at REAL)')
        db.execute('CREATE UNIQUE INDEX idx_uuid ON readings(record_uuid)')
        for row in rows:
            fields = list(protocol.SOURCE_FIELDS)
            db.execute('INSERT INTO readings('+','.join(fields)+',sync_error,sync_quarantined_at) VALUES('+','.join('?' for _ in fields)+',?,?)', [row[field] for field in fields]+['HTTP Error 422: rejected',time.time()])
        db.execute('CREATE TABLE alarm_events(id TEXT PRIMARY KEY,state TEXT)')
        db.execute("INSERT INTO alarm_events VALUES('alarm-preserve','active')")
        db.execute('CREATE TABLE thresholds(id TEXT PRIMARY KEY,value INTEGER)')
        db.execute("INSERT INTO thresholds VALUES('limit-preserve',1234)")


class MaintenanceProtocolTests(unittest.TestCase):
    def setUp(self):
        self.private = Ed25519PrivateKey.generate()
        self.keys = {protocol.KEY_ID:self.private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex()}
        self.payload = {'protocol':1,'kind':'request','action':'retry_reading','task_id':str(uuid4()),
                        'customer_id':'customer-a','site_id':'site-a','instance_id':'collector-fixture-instance',
                        'record_uuid':uuid4().hex,'raw_sha256':'a'*64,'issued_at':1000,'expires_at':1300}

    def verify(self, envelope, **values):
        return protocol.verify(envelope,customer_id='customer-a',site_id='site-a',instance_id='collector-fixture-instance',kind='request',keys=self.keys,now=1100,**values)

    def test_signature_and_scope_are_required(self):
        signed = protocol.signed(self.payload,self.private)
        self.assertEqual(self.verify(signed),self.payload)
        for field,value in [('customer_id','other'),('site_id','other'),('instance_id','other'),('action','run_script')]:
            payload={**self.payload,field:value}
            with self.subTest(field=field),self.assertRaises(ValueError):
                self.verify(protocol.signed(payload,self.private))
        tampered=copy.deepcopy(signed);tampered['payload']['raw_sha256']='b'*64
        with self.assertRaises(InvalidSignature):self.verify(tampered)

    def test_expiry_future_clock_unknown_keys_and_arbitrary_fields_fail_closed(self):
        for change in [{'expires_at':1001},{'issued_at':1200,'expires_at':1500},{'expires_at':2000},{'shell':'powershell'}]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.verify(protocol.signed({**self.payload,**change},self.private))
        envelope=protocol.signed(self.payload,self.private,key_id='unknown')
        with self.assertRaises(KeyError):self.verify(envelope)

    def test_nonfinite_original_is_inspectable_but_cannot_pass_transport_validation(self):
        row=reading(particles_json='{"pm_0_5_um":NaN}')
        raw=protocol.source_text(row)
        self.assertIn('NaN',raw)
        with self.assertRaises(ValueError):protocol.reading_payload(raw)


class CollectorMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='maintenance-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'fixture.sqlite3'
        self.row=reading();create_local(self.path,[self.row])
        self.logs=[]
        self.service=CloudSyncService(self.path,'https://example.invalid','fixture-token-only','site-a',lambda *values:self.logs.append(values))
        self.service.customer_id='customer-a'
        self.private=Ed25519PrivateKey.generate()
        self.keys={protocol.KEY_ID:self.private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex()}
        self.client=CollectorMaintenance(self.service,trusted_keys=self.keys)
        self.config=Path(self.temp.name)/'collector_settings.json';self.config.write_text('fixture configuration remains unchanged')

    def state(self):
        with self.service.connect() as db:return dict(db.execute('SELECT * FROM readings WHERE record_uuid=?',(self.row['record_uuid'],)).fetchone())

    def task(self, **values):
        now=int(time.time())
        task={'protocol':1,'kind':'request','action':'retry_reading','task_id':str(uuid4()),'customer_id':'customer-a','site_id':'site-a',
              'instance_id':self.service.instance_id,'record_uuid':self.row['record_uuid'],'raw_sha256':protocol.raw_digest(protocol.source_text(self.row)),
              'issued_at':now-1,'expires_at':now+protocol.TASK_TTL-1}
        task.update(values);return task

    def receipt(self, task):
        now=int(time.time())
        return protocol.signed({**task,'kind':'receipt','issued_at':now,'expires_at':now+protocol.TASK_TTL,'accepted_at':now},self.private)

    def test_snapshot_is_read_only_scoped_redacted_and_payload_bounded(self):
        original=self.state()
        with self.service.connect() as db:
            db.execute('UPDATE readings SET sync_error=?',('HTTP 422 Bearer secret password=hidden fixture-token-only',))
        report=self.client.snapshot()
        self.assertEqual(report['total'],1);self.assertTrue(report['complete'])
        text=json.dumps(report)
        for secret in ['fixture-token-only','Bearer secret','password=hidden']:self.assertNotIn(secret,text)
        self.assertEqual(protocol.raw_digest(report['items'][0]['raw_text']),report['items'][0]['raw_sha256'])
        self.assertEqual(self.state()['sync_quarantined_at'],original['sync_quarantined_at'])
        with self.service.connect() as db:db.execute('UPDATE readings SET particles_json=?',('x'*(protocol.MAX_RAW_BYTES*2),))
        item=self.client.snapshot()['items'][0]
        self.assertIsNone(item['raw_text']);self.assertIsNone(item['raw_sha256']);self.assertTrue(item['unavailable_reason'])
        self.assertEqual(len(self.state()['particles_json']),protocol.MAX_RAW_BYTES*2)

    def test_backup_precedes_upload_and_only_verified_receipt_updates_sync_metadata(self):
        task=self.task();original=self.state();calls=[]
        def request(path,payload=None):
            calls.append(path)
            if path.endswith('/snapshot'):return {'ok':True}
            if path.endswith('/tasks'):return {'ok':True,'data':[{'state':'pending','task':protocol.signed(task,self.private)}]}
            if path.endswith('/execute'):
                self.assertEqual(self.state(),original)
                with self.service.connect() as db:
                    backup=db.execute('SELECT original_row_json FROM maintenance_reading_backups WHERE task_id=?',(task['task_id'],)).fetchone()
                self.assertEqual(json.loads(backup[0]),original)
                return {'ok':True,'data':{'receipt':self.receipt(task)}}
            return {'ok':True}
        self.client.request=request
        self.assertEqual(self.client.once(),1)
        result=self.state();self.assertIsNone(result['sync_quarantined_at']);self.assertIsNone(result['sync_error']);self.assertIsNotNone(result['synced_at'])
        for key in original.keys()-{'synced_at','sync_quarantined_at','sync_error'}:self.assertEqual(result[key],original[key])
        with self.service.connect() as db:
            self.assertEqual(db.execute('SELECT * FROM alarm_events').fetchall()[0]['state'],'active')
            self.assertEqual(db.execute('SELECT value FROM thresholds').fetchone()[0],1234)
        self.assertEqual(self.config.read_text(),'fixture configuration remains unchanged')

    def test_upload_failure_preserves_record_and_backup_and_has_no_local_success(self):
        task=self.task();original=self.state()
        self.client.prepare(task)
        self.assertEqual(self.state(),original)
        with self.service.connect() as db:self.assertEqual(db.execute('SELECT state FROM maintenance_reading_backups').fetchone()[0],'prepared')
        self.client.request=lambda *_args,**_kw: (_ for _ in ()).throw(TimeoutError('disconnected'))
        with self.assertRaises(TimeoutError):self.client.once()
        self.assertEqual(self.state(),original)

    def test_large_quarantine_rotates_reports_and_failed_report_keeps_its_page(self):
        with self.service.connect() as db:
            for _ in range(protocol.MAX_REPORT_ITEMS * 2):
                fields=list(protocol.SOURCE_FIELDS)
                row=reading()
                db.execute('INSERT INTO readings('+','.join(fields)+',sync_attempts,sync_quarantined_at) VALUES('+','.join('?' for _ in fields)+',5,1)',[row[field] for field in fields])
            expected={row[0] for row in db.execute('SELECT record_uuid FROM readings')}
        pages=[self.client.snapshot() for _ in range(3)]
        self.assertEqual({item['record_id'] for page in pages for item in page['items']},expected)
        self.assertTrue(all(not page['complete'] and page['total']==101 for page in pages))
        self.assertTrue(all(len(page['items'])<=protocol.MAX_REPORT_ITEMS for page in pages))
        cursor=self.client.snapshot_cursor
        self.client.request=lambda *_: (_ for _ in ()).throw(TimeoutError('failed report'))
        with self.assertRaises(TimeoutError):self.client.once()
        self.assertEqual(self.client.snapshot_cursor,cursor)

    def test_changed_wrong_scope_nonquarantined_and_ambiguous_uuid_are_not_sent(self):
        task=self.task()
        for change in [{'raw_sha256':'b'*64},{'record_uuid':uuid4().hex},{'customer_id':'other'},{'site_id':'other'},{'instance_id':'other'}]:
            with self.subTest(change=change),self.assertRaises(ValueError):self.client.prepare({**task,**change})
        with self.service.connect() as db:db.execute('UPDATE readings SET sync_quarantined_at=NULL')
        with self.assertRaises(ValueError):self.client.prepare(task)
        with self.service.connect() as db:
            db.execute('UPDATE readings SET sync_quarantined_at=1')
            fields=list(protocol.SOURCE_FIELDS)
            values=[str(UUID(self.row['record_uuid'])) if field=='record_uuid' else self.row[field] for field in fields]
            db.execute('INSERT INTO readings('+','.join(fields)+',sync_quarantined_at) VALUES('+','.join('?' for _ in fields)+',1)',values)
        with self.assertRaises(ValueError):self.client.prepare(task)

    def test_ack_lost_then_process_restart_recovers_without_duplicate_upload(self):
        task=self.task();self.client.prepare(task)
        self.client.request=lambda *_args: (_ for _ in ()).throw(OSError('ack lost'))
        with self.assertRaises(OSError):self.client.complete(self.receipt(task))
        completed=self.state()
        restarted=CollectorMaintenance(self.service,trusted_keys=self.keys)
        acknowledged=[];restarted.request=lambda path,payload: acknowledged.append(path) or {'ok':True}
        restarted.complete(self.receipt(task))
        self.assertEqual(self.state(),completed);self.assertEqual(len(acknowledged),1)
        # A subsequent normal archive must not prevent acknowledgement recovery.
        with self.service.connect() as db:db.execute('DELETE FROM readings')
        restarted.complete(self.receipt(task));self.assertEqual(len(acknowledged),2)

    def test_forged_receipt_or_changed_data_never_clears_quarantine(self):
        task=self.task();self.client.prepare(task);original=self.state()
        bad=self.receipt(task);bad['payload']['raw_sha256']='b'*64
        with self.assertRaises(InvalidSignature):self.client.complete(bad)
        self.assertEqual(self.state(),original)
        with self.service.connect() as db:db.execute('UPDATE readings SET particles_json=?',('{"pm_0_5_um":456}',))
        with self.assertRaises(ValueError):self.client.complete(self.receipt(task))
        self.assertIsNotNone(self.state()['sync_quarantined_at'])

    def test_busy_sqlite_is_bounded_and_does_not_change_normal_writer(self):
        task=self.task()
        with closing(sqlite3.connect(self.path,timeout=.2)) as other, other:
            other.execute('BEGIN IMMEDIATE');other.execute("UPDATE thresholds SET value=4321")
            started=time.monotonic()
            with self.assertRaises(sqlite3.OperationalError):self.client.prepare(task)
            self.assertLess(time.monotonic()-started,1)
        self.assertIsNotNone(self.state()['sync_quarantined_at'])

    def test_blocked_maintenance_does_not_block_latest_history_or_alarms(self):
        blocked,release,latest,history,alarms=[threading.Event() for _ in range(5)]
        self.service.pull_config_once=lambda:0
        self.service.heartbeat_once=lambda:0
        self.service.sync_diagnostics_once=lambda:0
        self.service.sync_latest_once=lambda:latest.set() or 0
        self.service.sync_once=lambda:history.set() or 0
        self.service.sync_alarms_once=lambda:alarms.set() or 0
        def slow():blocked.set();release.wait(3);return 0
        self.service.sync_maintenance_once=slow;self.service.remote_discovery_enabled=False
        self.service.start()
        try:
            self.assertTrue(blocked.wait(1));self.assertTrue(latest.wait(1));self.assertTrue(history.wait(1));self.assertTrue(alarms.wait(1))
        finally:release.set();self.service.stop()


if __name__=='__main__':unittest.main()
