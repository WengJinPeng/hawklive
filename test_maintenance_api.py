"""Real PostgreSQL + SQLite + authenticated API tests on synthetic data only."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import cloud_api as api
from cloud_sync import CloudSyncService
from collector_maintenance import CollectorMaintenance
import maintenance_protocol as protocol
from test_collector_maintenance import create_local, reading


@unittest.skipUnless(os.environ.get('DCP_MAINTENANCE_TEST_DATABASE_URL'), 'Isolated maintenanceqa PostgreSQL required')
class MaintenanceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn=os.environ['DCP_MAINTENANCE_TEST_DATABASE_URL']
        if not dsn.endswith('/maintenanceqa'):raise RuntimeError('Use the isolated maintenanceqa database')
        cls.environment=patch.dict(os.environ,{'DATABASE_URL':dsn});cls.environment.start()
        api.initialize_schema();api.initialize_schema()

    @classmethod
    def tearDownClass(cls):cls.environment.stop()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='maintenance-api-qa-')
        self.root=Path(self.temp.name)
        self.private=Ed25519PrivateKey.generate()
        public=self.private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex()
        self.key_path=self.root/'fixture-signing.pem'
        self.key_path.write_bytes(self.private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        self.keys=patch.object(protocol,'TRUSTED_KEYS',{protocol.KEY_ID:public});self.keys.start()
        self.key_env=patch.dict(os.environ,{'DCP_MAINTENANCE_SIGNING_KEY_FILE':str(self.key_path)});self.key_env.start()
        self.customer='maint-'+uuid4().hex[:12];self.site=self.customer+'-site';self.room=self.customer+'-room';self.device=self.customer+'-device'
        self.token=uuid4().hex;self.session=uuid4().hex;self.viewer=uuid4().hex;self.other=uuid4().hex
        self.user_id=str(uuid4())
        with api.connect() as db:
            db.execute('INSERT INTO customers(id,name) VALUES(%s,%s),(%s,%s)',(self.customer,'Synthetic maintenance QA',self.customer+'-other','Other synthetic QA'))
            db.execute('INSERT INTO sites(id,customer_id,name) VALUES(%s,%s,%s),(%s,%s,%s)',(self.site,self.customer,'Fixture node',self.site+'-other',self.customer+'-other','Other node'))
            db.execute('INSERT INTO cleanrooms(id,customer_id,name) VALUES(%s,%s,%s)',(self.room,self.customer,'Fixture room'))
            db.execute("INSERT INTO devices(id,customer_id,cleanroom_id,site_id,name,host) VALUES(%s,%s,%s,%s,'Fixture sensor','127.0.0.1')",(self.device,self.customer,self.room,self.site))
            db.execute('UPDATE device_assignment_periods SET started_at=to_timestamp(0) WHERE customer_id=%s',(self.customer,))
            db.execute("INSERT INTO edge_tokens(token_hash,customer_id,site_id,label) VALUES(%s,%s,%s,'isolated QA')",(hashlib.sha256(self.token.encode()).hexdigest(),self.customer,self.site))
            for session,role,customer,user_id in [(self.session,'customer_admin',self.customer,self.user_id),(self.viewer,'viewer',self.customer,str(uuid4())),(self.other,'customer_admin',self.customer+'-other',str(uuid4()))]:
                db.execute("INSERT INTO customer_users(id,customer_id,username,display_name,password_salt,password_hash,role) VALUES(%s,%s,%s,'Fixture operator','x','x',%s)",(user_id,customer,user_id,role))
                db.execute("INSERT INTO customer_sessions(token_hash,user_id,expires_at) VALUES(%s,%s,now()+interval '1 hour')",(hashlib.sha256(session.encode()).hexdigest(),user_id))
            db.execute("INSERT INTO alarm_events(event_uuid,customer_id,site_id,cleanroom_id,device_id,source,metric,started_at,limit_description) VALUES(%s,%s,%s,%s,%s,'device','fixture-alarm',now(),'Preserve this synthetic alarm')",(str(uuid4()),self.customer,self.site,self.room,self.device))
        self.row=reading(customer_id=self.customer,site_id=self.site,cleanroom_id=self.room,device_id=self.device)
        self.path=self.root/'fixture.sqlite3';create_local(self.path,[self.row])
        self.service=CloudSyncService(self.path,'http://testserver',self.token,self.site,lambda *_:None)
        self.service.customer_id=self.customer
        self.http=TestClient(api.app);self.http.cookies.set('dcp_session',self.session)
        self.service._request_json=self.edge_request
        self.client=CollectorMaintenance(self.service)
        payload={'site_id':self.site,'collector_time':time.time(),'version':'0.6.11','maintenance_protocol':1,'hostname':'Synthetic QA','platform':'test','monitor_running':True,'device_total':1,'device_online':1}
        response=self.http.post('/api/v1/edge/heartbeat',json=payload,headers=self.service._headers())
        self.assertEqual(response.status_code,200,response.text)

    def tearDown(self):
        self.http.close()
        customers=[self.customer,self.customer+'-other']
        with api.connect() as db:
            for table in ('collector_quarantine_tasks','collector_quarantine_records','collector_quarantine_reports','configuration_audit_events','collector_diagnostic_logs','email_notifications','email_alert_settings','alarm_events','readings','latest_readings','device_maintenance_periods','device_assignment_periods','device_registration_links','thresholds','devices','cleanrooms','edge_tokens'):
                db.execute('DELETE FROM '+table+' WHERE customer_id=ANY(%s)',(customers,))
            db.execute('DELETE FROM customer_users WHERE customer_id=ANY(%s)',(customers,))
            db.execute('DELETE FROM sites WHERE customer_id=ANY(%s)',(customers,))
            db.execute('DELETE FROM customers WHERE id=ANY(%s)',(customers,))
        self.keys.stop();self.key_env.stop();self.temp.cleanup()

    def edge_request(self, request):
        response=self.http.request(request.get_method(),urlsplit(request.full_url).path,content=request.data,headers=dict(request.header_items()))
        if response.status_code>=400:
            raise urllib.error.HTTPError(request.full_url,response.status_code,'QA response',response.headers,io.BytesIO(response.content))
        return response.json()

    def listing(self, session=None):
        self.http.cookies.set('dcp_session',session or self.session)
        return self.http.get('/api/admin/sites/'+self.site+'/quarantine')

    def report(self):
        self.client.request('/api/v1/edge/quarantine/snapshot',self.client.snapshot())
        return self.listing().json()['data']['items'][0]

    def validate(self, record):
        return self.http.post(f'/api/admin/sites/{self.site}/quarantine/{record["id"]}/validate',json={})

    def enqueue(self, record=None, request_id=None):
        record=record or self.report()
        response=self.validate(record);self.assertEqual(response.status_code,200,response.text)
        result=response.json()['data'];self.assertTrue(result['valid'],result)
        payload={'request_id':str(request_id or uuid4()),'validation_id':result['id'],'raw_sha256':record['raw_sha256'],'record_uuid':record['record_id']}
        response=self.http.post(f'/api/admin/sites/{self.site}/quarantine/{record["id"]}/retry',json=payload)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['data'],record,payload

    def local(self):
        with self.service.connect() as db:return dict(db.execute('SELECT * FROM readings WHERE record_uuid=?',(self.row['record_uuid'],)).fetchone())

    def fingerprint(self):
        with api.connect() as db:
            return {table:db.execute("SELECT md5(coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,'[]')) FROM "+table+' t WHERE customer_id=%s',(self.customer,)).fetchone()[0] for table in ('devices','customer_users','thresholds','alarm_events','latest_readings','email_notifications')}

    def test_snapshot_and_validation_preserve_data_configuration_and_tenant_boundaries(self):
        before=self.fingerprint();local=self.local();record=self.report()
        response=self.validate(record);self.assertTrue(response.json()['data']['valid'],response.text)
        self.assertEqual(self.fingerprint(),before);self.assertEqual(self.local(),local)
        self.assertEqual(self.listing(self.viewer).status_code,403)
        self.assertEqual(self.listing(self.other).status_code,404)
        self.http.cookies.clear();self.assertEqual(self.http.get('/api/admin/sites/'+self.site+'/quarantine').status_code,401)

    def test_exact_uuid_replay_and_repeated_request_have_one_cloud_row_and_retained_local_backup(self):
        before=self.fingerprint();local=self.local();job,record,payload=self.enqueue()
        duplicate=self.http.post(f'/api/admin/sites/{self.site}/quarantine/{record["id"]}/retry',json=payload)
        self.assertEqual(duplicate.json()['data']['id'],job['id'])
        self.client.last_report=time.monotonic()
        self.assertEqual(self.client.once(),1)
        self.assertEqual(self.fingerprint(),before)
        with api.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM readings WHERE record_uuid=%s',(str(UUID(self.row['record_uuid'])),)).fetchone()[0],1)
            self.assertEqual(db.execute('SELECT state FROM collector_quarantine_tasks WHERE id=%s',(job['id'],)).fetchone()[0],'completed')
        after=self.local();self.assertIsNotNone(after['synced_at']);self.assertIsNone(after['sync_quarantined_at'])
        for key in local.keys()-{'synced_at','sync_quarantined_at','sync_error'}:self.assertEqual(local[key],after[key])
        with self.service.connect() as db:self.assertEqual(json.loads(db.execute('SELECT original_row_json FROM maintenance_reading_backups').fetchone()[0]),local)
        self.assertEqual(self.client.once(),0)

    def test_lost_cloud_response_recovers_from_accepted_task_without_second_ingest(self):
        job,record,payload=self.enqueue();original=self.local();self.client.last_report=time.monotonic()
        actual=self.client.request
        def lose(path,payload=None):
            result=actual(path,payload)
            if path.endswith('/execute'):raise TimeoutError('synthetic lost reply after commit')
            return result
        self.client.request=lose
        with self.assertRaises(TimeoutError):self.client.once()
        self.assertEqual(self.local(),original)
        restarted=CollectorMaintenance(self.service);restarted.last_report=time.monotonic()
        with patch.object(api,'ingest_batch',wraps=api.ingest_batch) as ingest:
            self.assertEqual(restarted.once(),1);ingest.assert_not_called()
        with api.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM readings WHERE record_uuid=%s',(str(UUID(self.row['record_uuid'])),)).fetchone()[0],1)

    def test_unit_json_nonfinite_and_assignment_errors_are_visible_without_writes(self):
        changes=[('particle_unit_code',2),('particles_json','{"pm_0_5_um":NaN}'),('particles_json','broken-json'),('timestamp',-1)]
        for field,value in changes:
            with self.subTest(field=field,value=value):
                with self.service.connect() as db:db.execute('UPDATE readings SET '+field+'=?',(value,))
                record=self.report();response=self.validate(record)
                self.assertEqual(response.status_code,200,response.text);self.assertFalse(response.json()['data']['valid'])
                self.assertTrue(response.json()['data']['issues']);self.assertIsNotNone(self.local()['sync_quarantined_at'])
                with self.service.connect() as db:db.execute('UPDATE readings SET '+field+'=?',(self.row[field],))
        with api.connect() as db:db.execute('UPDATE device_assignment_periods SET started_at=to_timestamp(2000) WHERE device_id=%s',(self.device,))
        record=self.report();response=self.validate(record)
        self.assertFalse(response.json()['data']['valid']);self.assertIn('assignment',str(response.json()['data']['issues']).lower())
        with api.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM readings WHERE customer_id=%s',(self.customer,)).fetchone()[0],0)

    def test_foreign_snapshot_invalid_digest_viewer_retry_and_reused_request_conflict(self):
        snapshot=self.client.snapshot();bad=json.loads(json.dumps(snapshot));bad['items'][0]['raw_sha256']='a'*64
        with self.assertRaises(urllib.error.HTTPError) as error:self.client.request('/api/v1/edge/quarantine/snapshot',bad)
        self.assertEqual(error.exception.code,422)
        foreign=reading(customer_id=self.customer+'-other',site_id=self.site+'-other')
        bad=json.loads(json.dumps(snapshot));bad['items'][0]['raw_text']=protocol.source_text(foreign);bad['items'][0]['raw_sha256']=protocol.raw_digest(bad['items'][0]['raw_text'])
        with self.assertRaises(urllib.error.HTTPError) as error:self.client.request('/api/v1/edge/quarantine/snapshot',bad)
        self.assertEqual(error.exception.code,403)
        job,record,payload=self.enqueue();self.http.cookies.set('dcp_session',self.viewer)
        self.assertEqual(self.http.post(f'/api/admin/sites/{self.site}/quarantine/{record["id"]}/retry',json=payload).status_code,403)
        self.http.cookies.set('dcp_session',self.session)
        payload['raw_sha256']='b'*64
        self.assertEqual(self.http.post(f'/api/admin/sites/{self.site}/quarantine/{record["id"]}/retry',json=payload).status_code,409)

    def test_offline_unsupported_stale_or_changed_snapshot_cannot_enqueue(self):
        record=self.report();validation=self.validate(record).json()['data']
        payload={'request_id':str(uuid4()),'validation_id':validation['id'],'raw_sha256':record['raw_sha256'],'record_uuid':record['record_id']}
        for clause in ["last_heartbeat_at=now()-interval '5 minutes'","last_heartbeat_at=now(),collector_status=jsonb_set(collector_status,'{maintenance_protocol}','0'::jsonb)"]:
            with api.connect() as db:db.execute('UPDATE sites SET '+clause+' WHERE id=%s',(self.site,))
            self.assertEqual(self.http.post(f'/api/admin/sites/{self.site}/quarantine/{record["id"]}/retry',json=payload).status_code,409)
        with api.connect() as db:
            db.execute("UPDATE sites SET last_heartbeat_at=now(),collector_status=jsonb_set(collector_status,'{maintenance_protocol}','1'::jsonb) WHERE id=%s",(self.site,))
            db.execute("UPDATE collector_quarantine_records SET reported_at=now()-interval '6 minutes' WHERE id=%s",(record['id'],))
        self.assertEqual(self.http.post(f'/api/admin/sites/{self.site}/quarantine/{record["id"]}/retry',json=payload).status_code,409)

    def test_uuid_collision_and_midflight_changed_data_preserve_existing_values(self):
        job,record,payload=self.enqueue()
        other={**self.row,'particles_json':'{"pm_0_5_um":999}'}
        item=api.ReadingIn.model_validate(protocol.reading_payload(protocol.source_text(other)))
        api.ingest_batch(api.ReadingBatch(site_id=self.site,readings=[item]),{'customer_id':self.customer,'site_id':self.site})
        self.client.last_report=time.monotonic()
        self.assertEqual(self.client.once(),0);self.assertIsNotNone(self.local()['sync_quarantined_at'])
        with api.connect() as db:
            self.assertEqual(db.execute('SELECT particles FROM readings WHERE record_uuid=%s',(str(UUID(self.row['record_uuid'])),)).fetchone()[0],{'pm_0_5_um':999})
            self.assertEqual(db.execute('SELECT state FROM collector_quarantine_tasks WHERE id=%s',(job['id'],)).fetchone()[0],'failed')

    def test_changed_local_data_fails_task_without_clearing_quarantine_or_blocking_new_validation(self):
        job,record,payload=self.enqueue();self.client.last_report=time.monotonic()
        with self.service.connect() as db:db.execute('UPDATE readings SET particles_json=?',('{"pm_0_5_um":456}',))
        self.assertEqual(self.client.once(),0)
        self.assertIsNotNone(self.local()['sync_quarantined_at'])
        with api.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM collector_quarantine_tasks WHERE id=%s',(job['id'],)).fetchone()[0],'failed')
            self.assertEqual(db.execute('SELECT count(*) FROM readings WHERE customer_id=%s',(self.customer,)).fetchone()[0],0)
        new_job,_,_=self.enqueue();self.assertNotEqual(job['id'],new_job['id'])
        self.assertEqual(self.client.once(),1)

    def test_expired_task_and_new_collector_instance_cannot_execute_old_job(self):
        job,record,payload=self.enqueue()
        with api.connect() as db:db.execute("UPDATE collector_quarantine_tasks SET expires_at=now()-interval '1 second' WHERE id=%s",(job['id'],))
        result=self.client.request('/api/v1/edge/quarantine/tasks');self.assertEqual(result['data'],[])
        with self.assertRaises(urllib.error.HTTPError) as error:self.client.request('/api/v1/edge/quarantine/tasks/'+job['id']+'/execute',{'raw_text':record['raw_text']})
        self.assertEqual(error.exception.code,409)
        with api.connect() as db:db.execute("UPDATE sites SET active_collector_instance='collector-different-instance',active_collector_seen_at=now() WHERE id=%s",(self.site,))
        with self.assertRaises(urllib.error.HTTPError) as error:self.client.request('/api/v1/edge/quarantine/tasks')
        self.assertEqual(error.exception.code,409);self.assertIsNotNone(self.local()['sync_quarantined_at'])

    def test_original_cleanup_also_removes_maintenance_monitoring_copies(self):
        self.enqueue()
        response=self.http.post('/api/admin/data-cleanup',json={'scope':'all_monitoring','confirmation':'PURGE ALL'})
        self.assertEqual(response.status_code,200,response.text)
        with api.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM collector_quarantine_records WHERE customer_id=%s',(self.customer,)).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM collector_quarantine_tasks WHERE customer_id=%s',(self.customer,)).fetchone()[0],0)
        self.assertIsNotNone(self.local()['sync_quarantined_at'])


if __name__=='__main__':unittest.main()
