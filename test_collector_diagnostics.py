from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import cloud_api
from cloud_sync import CloudSyncService
from collector_diagnostics import enqueue, redact, prune_queue, diagnostic_scope


class DiagnosticQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'test.sqlite3'
        self.service = CloudSyncService(self.path, 'https://example.invalid', 'private-live-token', 'site-a', lambda *_: None)
        self.service.customer_id = 'customer-a'
        self.service._last_diagnostic_snapshot = time.monotonic()

    def tearDown(self):
        self.directory.cleanup()

    def queue(self, **kw):
        values = dict(customer_id='customer-a', site_id='site-a', instance_id=self.service.instance_id,
                      level='ERROR', event='device_offline', message='device-a: Socket closed')
        values.update(kw)
        with self.service.connect() as db:
            enqueue(db, **values)

    def count(self):
        with self.service.connect() as db:
            return db.execute('SELECT count(*) FROM diagnostic_outbox').fetchone()[0]

    def test_redaction_preserves_error_and_removes_credentials(self):
        raw = 'Socket closed; Bearer abcd; password="with spaces"; api_key=123; https://user:pass@example.test/a?signature=secret; opaque-private'
        value = redact(raw, ('opaque-private',))
        for secret in ('abcd', 'with spaces', '123', 'user:pass', 'signature=secret', 'opaque-private'):
            self.assertNotIn(secret, value)
        self.assertIn('Socket closed', value)
        self.assertNotIn('cookievalue', redact('Cookie: cookievalue'))
        self.assertLessEqual(len(redact('x'*3000)), 2000)

    def test_failure_and_incomplete_ack_keep_stable_ids_until_success(self):
        self.queue()
        captured = []
        def response(request):
            body = json.loads(request.data)
            captured.append(body)
            return {'ok': True, 'accepted': [item['record_uuid'] for item in body['logs']]}
        self.service._request_json = lambda _: (_ for _ in ()).throw(OSError('offline'))
        with self.assertRaises(OSError): self.service.sync_diagnostics_once()
        self.assertEqual(self.count(), 1)
        self.service._request_json = lambda _: {'ok': True, 'accepted': []}
        with self.assertRaises(RuntimeError): self.service.sync_diagnostics_once()
        with self.service.connect() as db:
            original = db.execute('SELECT record_uuid FROM diagnostic_outbox').fetchone()[0]
        restarted = CloudSyncService(self.path, 'https://example.invalid', 'token', 'site-a', lambda *_: None)
        restarted.customer_id = 'customer-a'
        restarted._last_diagnostic_snapshot = time.monotonic()
        restarted._request_json = response
        self.assertEqual(restarted.sync_diagnostics_once(), 1)
        self.assertEqual(captured[0]['logs'][0]['record_uuid'], original)
        self.assertEqual(self.count(), 0)

    def test_customer_and_site_change_never_rebind_old_logs(self):
        self.queue()
        self.queue(customer_id='other', site_id='other-site')
        self.service._request_json = lambda r: {'ok': True, 'accepted': [i['record_uuid'] for i in json.loads(r.data)['logs']]}
        self.assertEqual(self.service.sync_diagnostics_once(), 1)
        self.assertEqual(self.count(), 1)

    def test_pending_queue_is_bounded_and_expired_logs_are_pruned(self):
        with patch('collector_diagnostics.LOCAL_LOG_LIMIT', 2):
            for _ in range(4): self.queue()
            self.assertEqual(self.count(), 2)
            with self.service.connect() as db:
                db.execute('UPDATE diagnostic_outbox SET timestamp=1')
                prune_queue(db)
            self.assertEqual(self.count(), 0)

    def test_local_logger_captures_redacted_scoped_outbox(self):
        import dashboard_server
        with patch.object(dashboard_server, "DB_PATH", self.path), patch.object(dashboard_server, "CLOUD_SYNC", self.service):
            dashboard_server.init_db()
            dashboard_server.add_log("ERROR", "device_offline", "Socket closed private-live-token")
            with diagnostic_scope("old-customer", "old-site"):
                dashboard_server.add_log("ERROR", "device_offline", "old tenant in-flight task")
            self.assertEqual(self.count(), 1)
        with self.service.connect() as db:
            row = db.execute("SELECT * FROM diagnostic_outbox").fetchone()
            self.assertEqual(row["customer_id"], "customer-a")
            self.assertEqual(row["site_id"], "site-a")
            self.assertNotIn("private-live-token", row["message"])
            self.assertNotIn("private-live-token", db.execute("SELECT message FROM logs").fetchone()[0])

    def test_blocked_log_upload_does_not_block_other_channels(self):
        import threading
        blocked, release, heartbeat, latest = [threading.Event() for _ in range(4)]
        def upload():
            blocked.set()
            release.wait(3)
            raise OSError("offline")
        self.service.sync_diagnostics_once = upload
        self.service.pull_config_once = lambda: 0
        self.service.heartbeat_once = lambda: heartbeat.set() or 0
        self.service.sync_latest_once = lambda: latest.set() or 0
        self.service.sync_once = lambda: 0
        self.service.sync_alarms_once = lambda: 0
        self.service.remote_discovery_enabled = False
        self.service.start()
        try:
            self.assertTrue(blocked.wait(1))
            self.assertTrue(heartbeat.wait(1))
            self.assertTrue(latest.wait(1))
        finally:
            release.set()
            self.service.stop()

    def test_configuration_apply_gate_does_not_hide_diagnostics(self):
        import threading
        self.service.config_applier = lambda _: None
        seen = threading.Event()
        worker = threading.Thread(target=self.service._run_channel,
                                  args=("diagnostics", lambda: seen.set() or 0, 30))
        worker.start()
        try:
            self.assertTrue(seen.wait(1), "Configuration-pending diagnostics must remain available")
        finally:
            self.service._stop.set()
            worker.join(2)

    def test_snapshot_allows_only_operational_fields(self):
        self.service._last_diagnostic_snapshot = 0
        self.service.status_provider = lambda: {'poll_seconds': 600, 'password': 'must-not-send', 'storage': {'state':'ok', 'token':'not-this-either'}}
        captured = []
        def response(request):
            captured.append(json.loads(request.data))
            return {'ok': True, 'accepted': [i['record_uuid'] for i in captured[-1]['logs']]}
        self.service._request_json = response
        self.service.sync_diagnostics_once()
        self.assertIn('600', captured[0]['logs'][0]['message'])
        self.assertNotIn('must-not-send', json.dumps(captured))
        self.assertNotIn('not-this-either', json.dumps(captured))


@unittest.skipUnless(os.environ.get('DCP_DIAGNOSTICS_TEST_DATABASE_URL'), 'Isolated diagnosticsqa PostgreSQL required')
class DiagnosticPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = os.environ['DCP_DIAGNOSTICS_TEST_DATABASE_URL']
        if not dsn.endswith('/diagnosticsqa'): raise RuntimeError('Use isolated diagnosticsqa database')
        cls.env = patch.dict(os.environ, {'DATABASE_URL': dsn})
        cls.env.start()
        cloud_api.initialize_schema()
        cloud_api.initialize_schema()

    @classmethod
    def tearDownClass(cls): cls.env.stop()

    def setUp(self):
        self.customer = 'diag-' + uuid4().hex
        self.site = self.customer + '-site'
        self.other_site = self.customer + '-other'
        self.token = uuid4().hex
        self.session = uuid4().hex
        self.viewer_session = uuid4().hex
        self.other_session = uuid4().hex
        with cloud_api.connect() as db:
            db.execute('INSERT INTO customers(id,name) VALUES(%s,%s),(%s,%s)', (self.customer,'QA',self.customer+'-other','Other QA'))
            db.execute('INSERT INTO sites(id,customer_id,name) VALUES(%s,%s,%s),(%s,%s,%s)', (self.site,self.customer,'QA node',self.other_site,self.customer+'-other','Other node'))
            db.execute("INSERT INTO edge_tokens(token_hash,customer_id,site_id,label) VALUES(%s,%s,%s,'QA')", (hashlib.sha256(self.token.encode()).hexdigest(),self.customer,self.site))
            for session, role, customer in [(self.session,'customer_admin',self.customer),(self.viewer_session,'viewer',self.customer),(self.other_session,'customer_admin',self.customer+'-other')]:
                uid = str(uuid4())
                db.execute("INSERT INTO customer_users(id,customer_id,username,display_name,password_salt,password_hash,role) VALUES(%s,%s,%s,'QA','x','x',%s)",(uid,customer,uid,role))
                db.execute("INSERT INTO customer_sessions(token_hash,user_id,expires_at) VALUES(%s,%s,now()+interval '1 hour')",(hashlib.sha256(session.encode()).hexdigest(),uid))
        self.http = TestClient(cloud_api.app)
        self.headers = {'Authorization': 'Bearer '+self.token,'X-DCP-Collector-Instance':'diagnostics-test-instance'}

    def tearDown(self):
        self.http.close()
        with cloud_api.connect() as db:
            for table in ('collector_diagnostic_logs','edge_tokens'):
                db.execute(f'DELETE FROM {table} WHERE customer_id=ANY(%s)',([self.customer,self.customer+'-other'],))
            db.execute('DELETE FROM customer_users WHERE customer_id=ANY(%s)',([self.customer,self.customer+'-other'],))
            db.execute('DELETE FROM sites WHERE customer_id=ANY(%s)',([self.customer,self.customer+'-other'],))
            db.execute('DELETE FROM customers WHERE id=ANY(%s)',([self.customer,self.customer+'-other'],))

    def batch(self, message='Socket closed; device=QA-01'):
        return {'site_id':self.site, 'logs':[{'record_uuid':str(uuid4()), 'timestamp':time.time(), 'level':'ERROR','event':'device_offline','message':message}]}

    def get(self, session=None, **params):
        self.http.cookies.set('dcp_session', session or self.session)
        return self.http.get(f'/api/admin/sites/{self.site}/diagnostics', params=params)

    def test_upload_deduplicates_and_defends_tenant_and_manager_boundaries(self):
        payload=self.batch('Socket closed; password=secretvalue')
        self.assertEqual(self.http.post('/api/v1/edge/diagnostics/batch',json=payload).status_code,401)
        for _ in range(2):
            self.assertEqual(self.http.post('/api/v1/edge/diagnostics/batch',json=payload,headers=self.headers).status_code,200)
        rows=self.get().json()['data']['items']
        self.assertEqual(len(rows),1)
        self.assertNotIn('secretvalue',rows[0]['message'])
        self.assertEqual(self.get(self.viewer_session).status_code,403)
        self.assertEqual(self.get(self.other_session).status_code,404)
        payload['site_id']=self.other_site
        self.assertEqual(self.http.post('/api/v1/edge/diagnostics/batch',json=payload,headers=self.headers).status_code,403)
        self.assertEqual(self.http.post('/api/v1/edge/diagnostics/batch',json=self.batch(),headers={**self.headers,'X-DCP-Collector-Instance':'competing-instance'}).status_code,409)

    def test_filters_pagination_and_clock_skew_use_receive_time(self):
        for message in ['device-a','device-b','device-a']:
            payload=self.batch(message)
            payload['logs'][0]['timestamp']=1  # Old device clock must still be visible after upload.
            self.http.post('/api/v1/edge/diagnostics/batch',json=payload,headers=self.headers)
        first=self.get(q='device-a',level='ERROR',limit=1).json()['data']
        second=self.get(q='device-a',level='ERROR',limit=1,before=first['next_before']).json()['data']
        self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
        self.assertIsNone(second['next_before'])
        self.assertEqual(self.get(q='absent').json()['data']['items'],[])
        self.assertEqual(self.get(level='INFO').json()['data']['items'],[])
        self.assertEqual(self.get(hours=1000).status_code,422)

    def test_old_received_logs_are_pruned_on_next_upload(self):
        self.http.post('/api/v1/edge/diagnostics/batch',json=self.batch(),headers=self.headers)
        with cloud_api.connect() as db:
            db.execute("UPDATE collector_diagnostic_logs SET received_at=now()-interval '31 days' WHERE site_id=%s",(self.site,))
        self.http.post('/api/v1/edge/diagnostics/batch',json=self.batch('new'),headers=self.headers)
        self.assertEqual(len(self.get(hours=720).json()['data']['items']),1)

    def test_heartbeat_and_device_connection_states_are_independent_and_recover(self):
        payload = dict(site_id=self.site, collector_time=time.time(),version='0.5.3',hostname='QA',platform='test',monitor_running=True,
                       device_total=2,device_online=0,storage_state='warning',
                       device_states=[{'device_id':'a','state':'offline','checked_at':time.time()},
                                      {'device_id':'b','state':'unknown'}])
        def read_site():
            self.http.cookies.set('dcp_session',self.session)
            return next(item for item in self.http.get('/api/admin/sites').json()['data']['sites'] if item['id']==self.site)
        self.assertEqual(self.http.post('/api/v1/edge/heartbeat',json=payload,headers=self.headers).status_code,200)
        site = read_site()
        self.assertTrue(site['connected'])
        self.assertEqual((site['device_online'],site['device_offline'],site['device_unknown']),(0,1,1))
        with cloud_api.connect() as db:
            db.execute("UPDATE sites SET last_heartbeat_at=now()-interval '100 seconds' WHERE id=%s",(self.site,))
        site = read_site()
        self.assertFalse(site['connected'])
        self.assertEqual((site['device_online'],site['device_offline'],site['device_unknown']),(0,0,2))
        payload['device_online']=2
        for item in payload['device_states']: item['state']='online'
        self.assertEqual(self.http.post('/api/v1/edge/heartbeat',json=payload,headers=self.headers).status_code,200)
        self.assertEqual(read_site()['device_online'],2)
