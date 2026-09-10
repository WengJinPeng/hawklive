"""Collector retirement against the explicitly opted-in disposable QA database."""
import hashlib
import os
import time
import unittest
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient
import cloud_api
import test_device_deletion_postgres as fixtures


@unittest.skipUnless(os.environ.get('DCP_DEVICE_DELETE_TEST_DATABASE_URL'), 'Opt-in isolated PostgreSQL required')
class CollectorRetirementTests(unittest.TestCase):
    setUp = fixtures.DeviceDeletionPostgresTests.setUp
    tearDown = fixtures.DeviceDeletionPostgresTests.tearDown

    @classmethod
    def setUpClass(cls):
        fixtures.DeviceDeletionPostgresTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.DeviceDeletionPostgresTests.tearDownClass.__func__(cls)

    @property
    def unused(self):
        return self.user['customer_id'] + '-other'

    def retire(self, site=None):
        return self.client.post('/api/admin/sites/' + (site or self.unused) + '/retire')

    def test_retire_retains_history_revokes_tokens_and_is_idempotent(self):
        site = self.payload['site_id']
        raw = uuid4().hex
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        identity = {'customer_id': self.user['customer_id'], 'site_id': site}
        record = dict(customer_id=identity['customer_id'], site_id=site, device_id=self.device_id,
                      cleanroom_id=self.payload['cleanroom_id'], cleanroom_name='History', device_name='Device',
                      measured_at=time.time(), source='device', particles={}, environment={}, alarm_status='normal', record_uuid=uuid4())
        cloud_api.ingest_batch(cloud_api.ReadingBatch(site_id=site, readings=[record]), identity)
        with cloud_api.connect() as db:
            db.execute('INSERT INTO edge_tokens(token_hash,customer_id,site_id,label) VALUES(%s,%s,%s,%s)',
                       (token_hash, identity['customer_id'], site, 'retire QA'))
        self.assertEqual(self.client.delete(self.url).status_code, 200)
        history = self.client.get('/api/history').json()['data']
        self.assertTrue(history)
        self.assertEqual(self.retire(site).status_code, 200)
        self.assertEqual(self.retire(site).status_code, 200)
        result = self.client.get('/api/admin/sites').json()['data']
        self.assertNotIn(site, [x['id'] for x in result['sites']])
        self.assertIn(site, [x['id'] for x in result['retired_sites']])
        self.assertEqual(history, self.client.get('/api/history').json()['data'])
        self.assertEqual(self.client.get('/api/v1/edge/config', headers={'Authorization': 'Bearer '+raw}).status_code, 401)
        # Restoring a deleted device may not attach it to a retired node.
        self.assertEqual(self.client.post(self.url+'/restore').status_code, 404)
        with cloud_api.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM configuration_audit_events WHERE target_id=%s AND action='collector.retired'", (site,)).fetchone()[0], 1)
            self.assertFalse(db.execute('SELECT enabled FROM edge_tokens WHERE token_hash=%s', (token_hash,)).fetchone()[0])

    def test_active_devices_and_recent_contacts_block_retirement(self):
        self.assertEqual(self.retire(self.payload['site_id']).status_code, 409)
        for column in ('last_heartbeat_at', 'active_collector_seen_at'):
            with cloud_api.connect() as db:
                db.execute(f'UPDATE sites SET {column}=now() WHERE id=%s', (self.unused,))
            self.assertEqual(self.retire().status_code, 409)
            row = next(x for x in self.client.get('/api/admin/sites').json()['data']['sites'] if x['id']==self.unused)
            self.assertFalse(row['can_retire'])
            with cloud_api.connect() as db:
                db.execute(f'UPDATE sites SET {column}=NULL WHERE id=%s', (self.unused,))
        with cloud_api.connect() as db:
            db.execute("INSERT INTO edge_tokens(token_hash,customer_id,site_id,label,last_used_at) VALUES(%s,%s,%s,'legacy',now())", (uuid4().hex,self.user['customer_id'],self.unused))
        self.assertEqual(self.retire().status_code, 409)

    def test_authorization_and_tenant_isolation(self):
        with TestClient(cloud_api.app) as anon:
            self.assertEqual(anon.post('/api/admin/sites/'+self.unused+'/retire').status_code,401)
        with cloud_api.connect() as db:
            db.execute("UPDATE customer_users SET role='viewer' WHERE id=%s",(self.user['id'],))
        self.assertEqual(self.retire().status_code,403)
        other = fixtures.seed_customer('Other')
        self.client.post('/api/login',json={'username':other['user']['customer_id'],'password':'local-delete-qa'})
        self.assertEqual(self.retire().status_code,404)
        self.assertEqual(self.retire('missing').status_code,404)

    def test_retired_site_cannot_receive_assignments_or_activation(self):
        with patch.object(cloud_api,'activation_secret_bytes',return_value=b'qa-secret'):
            token = cloud_api.reissue_collector_activation(self.user['customer_id'],self.unused,actor_user_id=self.user['id'])
            self.assertEqual(self.retire().status_code,200)
            response=self.client.post('/api/v1/edge/activate',json={'activation_token':token,'machine_id':uuid4().hex,'hostname':'QA','platform':'Windows'})
            self.assertEqual(response.status_code,403,response.text)
        response=self.client.post('/api/admin/devices',json={**self.payload,'site_id':self.unused,'name':'Other'})
        self.assertEqual(response.status_code,404,response.text)
        current=next(d for d in self.client.get('/api/admin/device-lifecycle').json()['data']['devices'] if d['id']==self.device_id)
        self.assertEqual(self.client.patch(self.url,json={'name':self.payload['name'],'cleanroom_id':self.payload['cleanroom_id'],'site_id':self.unused,'expected_updated_at':current['updated_at']}).status_code,404)
        # Retirement releases active capacity without reusing historical identities.
        with patch.object(cloud_api,'MAX_COLLECTORS',2):
            created=cloud_api.create_cloud_collector(self.user['customer_id'],'Replacement',actor_user_id=self.user['id'])
            self.assertNotEqual(created['collector']['id'],self.unused)

    def test_failed_audit_rolls_back_retirement_and_token_revocation(self):
        raw=uuid4().hex
        with cloud_api.connect() as db:
            db.execute("INSERT INTO edge_tokens(token_hash,customer_id,site_id,label) VALUES(%s,%s,%s,'rollback')",(raw,self.user['customer_id'],self.unused))
        with patch.object(cloud_api,'record_configuration_audit',side_effect=RuntimeError('audit unavailable')):
            self.assertEqual(self.retire().status_code,500)
        with cloud_api.connect() as db:
            self.assertIsNone(db.execute('SELECT retired_at FROM sites WHERE id=%s',(self.unused,)).fetchone()[0])
            self.assertTrue(db.execute('SELECT enabled FROM edge_tokens WHERE token_hash=%s',(raw,)).fetchone()[0])

    def test_assignment_and_removal_race_cannot_leave_active_device_on_retired_site(self):
        from concurrent.futures import ThreadPoolExecutor
        def assign():
            try:
                cloud_api.create_cloud_device(self.user['customer_id'],{**self.payload,'site_id':self.unused,'name':'Racing device'},actor_kind='customer_user',actor_user_id=self.user['id'])
                return 201
            except cloud_api.HTTPException as exc:
                return exc.status_code
        def retire():
            try:
                cloud_api.retire_collector(self.unused,self.user)
                return 200
            except cloud_api.HTTPException as exc:
                return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            one=pool.submit(assign); two=pool.submit(retire)
            self.assertIn((one.result(),two.result()),((201,409),(404,200)))
        with cloud_api.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM devices d JOIN sites s ON s.id=d.site_id WHERE d.customer_id=%s AND d.enabled AND s.retired_at IS NOT NULL',(self.user['customer_id'],)).fetchone()[0],0)

    def test_enrolled_machine_cannot_reclaim_retired_credentials(self):
        with patch.object(cloud_api,'activation_secret_bytes',return_value=b'qa-secret'):
            customer=self.user['customer_id']; machine=uuid4().hex; secret='qa-claim-secret-'*3
            token=cloud_api.customer_enrollment_token(customer,1)
            cloud_api.register_collector_enrollment(customer,token,machine,secret,'QA','Windows')
            pending=cloud_api.admin_pending_collectors(self.user)['data'][0]
            approved=cloud_api.approve_collector_enrollment(customer,pending['id'],'Enrolled QA',actor_user_id=self.user['id'])
            self.assertEqual(self.retire(approved['id']).status_code,200)
            with self.assertRaises(cloud_api.HTTPException) as denied:
                cloud_api.register_collector_enrollment(customer,token,machine,secret,'QA','Windows')
            self.assertEqual(denied.exception.status_code,403)
