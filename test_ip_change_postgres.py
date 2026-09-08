"""IP is a mutable endpoint, never implicit evidence of physical identity."""
import os,time,unittest
from uuid import uuid4
from unittest.mock import patch
import cloud_api
import test_device_deletion_postgres as fixtures


@unittest.skipUnless(os.environ.get('DCP_DEVICE_DELETE_TEST_DATABASE_URL'),'Opt-in isolated PostgreSQL required')
class IpChangePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.DeviceDeletionPostgresTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):fixtures.DeviceDeletionPostgresTests.tearDownClass.__func__(cls)
    tearDown=fixtures.DeviceDeletionPostgresTests.tearDown
    versions=fixtures.DeviceDeletionPostgresTests.versions
    def setUp(self):
        fixtures.DeviceDeletionPostgresTests.setUp(self)
        self.payload['host']='10.12.0.14'
        with cloud_api.connect() as db:db.execute('UPDATE devices SET host=%s WHERE id=%s',(self.payload['host'],self.device_id))
    def items(self):return self.client.get('/api/admin/device-lifecycle').json()['data']['devices']
    def body(self,duplicate=None):
        items={d['id']:d for d in self.items()}
        return dict(host='10.12.0.140',tcp_port=502,slave=1,expected_updated_at=items[self.device_id]['updated_at'],confirmed_same_device=True,
                    duplicate_device_id=duplicate,duplicate_updated_at=items[duplicate]['updated_at'] if duplicate else None)
    def duplicate(self,other_site=False):
        room=cloud_api.create_cloud_cleanroom(self.user['customer_id'],'Duplicate room',actor_kind='customer_user',actor_user_id=self.user['id'])
        return cloud_api.create_cloud_device(self.user['customer_id'],dict(self.payload,host='10.12.0.140',name='Duplicate registration',cleanroom_id=room,site_id=self.user['customer_id']+'-other' if other_site else self.payload['site_id']),actor_kind='customer_user',actor_user_id=self.user['id'])
    def ingest(self,device_id):
        with cloud_api.connect() as db:site,room=db.execute('SELECT site_id,cleanroom_id FROM devices WHERE id=%s',(device_id,)).fetchone()
        record=dict(record_uuid=uuid4(),customer_id=self.user['customer_id'],site_id=site,cleanroom_id=room,cleanroom_name='QA',device_id=device_id,device_name='QA',measured_at=time.time(),source='device',particles={},environment={},alarm_status='normal')
        cloud_api.ingest_batch(cloud_api.ReadingBatch(site_id=site,readings=[record]),{'customer_id':self.user['customer_id'],'site_id':site})
    def test_changed_ip_keeps_identity_and_single_active_registration(self):
        self.ingest(self.device_id);before=self.versions()
        body=self.body();r=self.client.post(self.url+'/rebind',json=body);self.assertEqual(r.status_code,200,r.text)
        active=[d for d in self.items() if d['enabled']]
        self.assertEqual(len(active),1);self.assertEqual(active[0]['id'],self.device_id);self.assertEqual(active[0]['host'],'10.12.0.140')
        self.assertEqual(len(self.client.get('/api/history').json()['data']),1)
        self.assertEqual(self.versions()[self.payload['site_id']],before[self.payload['site_id']]+1)
        with cloud_api.connect() as db:
            details=db.execute("SELECT details FROM configuration_audit_events WHERE target_id=%s AND action='device.address_changed'",(self.device_id,)).fetchone()[0]
        self.assertEqual(details['identity_evidence'],'operator_confirmation')
        versions=self.versions();self.assertEqual(self.client.post(self.url+'/rebind',json=body).status_code,200);self.assertEqual(versions,self.versions())
    def test_confirmed_duplicate_is_retired_without_rewriting_history_and_retry_is_idempotent(self):
        duplicate=self.duplicate();self.ingest(self.device_id);self.ingest(duplicate)
        body=self.body(duplicate);before=self.versions()
        result=self.client.post(self.url+'/rebind',json=body);self.assertEqual(result.status_code,200,result.text)
        items={d['id']:d for d in self.items()}
        self.assertEqual(sum(d['enabled'] for d in items.values()),1)
        self.assertEqual(items[duplicate]['linked_to'],self.device_id)
        self.assertEqual(set(items[self.device_id]['related_device_ids']),{self.device_id,duplicate})
        history=self.client.get('/api/history',params={'device_ids':','.join(items[self.device_id]['related_device_ids'])}).json()['data']
        self.assertEqual({d['device_id'] for d in history},{self.device_id,duplicate})
        versions=self.versions();self.assertEqual(versions[self.payload['site_id']],before[self.payload['site_id']]+1)
        self.assertEqual(self.client.post(self.url+'/rebind',json=body).status_code,200);self.assertEqual(versions,self.versions())
        self.assertEqual(self.client.post('/api/admin/devices/'+duplicate+'/restore').status_code,409)
        self.assertEqual(len(self.client.get('/api/config').json()['data'][0]['devices']),1)
    def test_unconfirmed_conflicting_and_stale_requests_never_merge(self):
        duplicate=self.duplicate();body=self.body(duplicate)
        self.assertEqual(self.client.post(self.url+'/rebind',json={**body,'confirmed_same_device':False}).status_code,400)
        self.assertEqual(self.client.post(self.url+'/rebind',json=self.body()).status_code,409)
        self.assertEqual(self.client.post(self.url+'/rebind',json={**body,'duplicate_updated_at':'old'}).status_code,409)
        self.assertEqual(self.client.post(self.url+'/rebind',json={**body,'host':'10.12.0.141'}).status_code,409)
        self.assertEqual(sum(d['enabled'] for d in self.items()),2)
    def test_failure_after_retirement_rolls_back_both_registrations_and_links(self):
        duplicate=self.duplicate();body=self.body(duplicate);before=self.versions()
        with patch.object(cloud_api,'record_configuration_audit',side_effect=RuntimeError('QA failure')):
            self.assertEqual(self.client.post(self.url+'/rebind',json=body).status_code,500)
        self.assertEqual(sum(d['enabled'] for d in self.items()),2);self.assertEqual(self.versions(),before)
        self.assertTrue(all(not d['linked_to'] for d in self.items()))
    def test_roles_cross_tenant_and_cross_collector_are_rejected(self):
        other=fixtures.seed_customer('Other tenant');body=self.body()
        self.assertEqual(self.client.post('/api/admin/devices/'+other['device_id']+'/rebind',json=body).status_code,404)
        self.assertEqual(self.client.post(self.url+'/rebind',json={**body,'duplicate_device_id':other['device_id'],'duplicate_updated_at':'QA'}).status_code,404)
        duplicate=self.duplicate(other_site=True)
        self.assertEqual(self.client.post(self.url+'/rebind',json=self.body(duplicate)).status_code,409)
        for role in ('customer','viewer'):
            with cloud_api.connect() as db:db.execute('UPDATE customer_users SET role=%s WHERE id=%s',(role,self.user['id']))
            self.assertEqual(self.client.post(self.url+'/rebind',json=body).status_code,403)
    def test_similar_addresses_remain_distinct_until_explicit_confirmation(self):
        self.duplicate()
        items=self.items();self.assertEqual(sum(d['enabled'] for d in items),2)
        self.assertTrue(all(not d['linked_to'] for d in items))

    def test_stale_collector_inventory_cannot_restore_a_linked_duplicate(self):
        duplicate=self.duplicate()
        old=cloud_api.configuration_for_site(cloud_api.customer_configuration(self.user['customer_id']),self.payload['site_id'])
        self.assertEqual(self.client.post(self.url+'/rebind',json=self.body(duplicate)).status_code,200)
        with self.assertRaises(cloud_api.HTTPException):
            cloud_api.apply_customer_config(cloud_api.CustomerSettings(rooms=old),self.user,allow_connection_changes=True,forced_site_id=self.payload['site_id'])
        self.assertEqual(sum(d['enabled'] for d in self.items()),1)
        self.assertTrue(next(d for d in self.items() if d['id']==duplicate)['linked_to'])

    def test_collector_saves_changed_endpoint_under_existing_id_and_applies_cloud_result(self):
        import tempfile
        from pathlib import Path
        import dashboard_server
        from monitoring_service import MonitoringService
        cid=self.user['customer_id'];sid=self.payload['site_id']
        config=cloud_api.configuration_for_site(cloud_api.customer_configuration(cid),sid)
        config[0]['devices'][0]['host']='10.12.0.140'
        cloud_api.apply_customer_config(cloud_api.CustomerSettings(rooms=config),self.user,allow_connection_changes=True,forced_site_id=sid)
        active=[d for d in self.items() if d['enabled']]
        self.assertEqual([(d['id'],d['host']) for d in active],[(self.device_id,'10.12.0.140')])
        with tempfile.TemporaryDirectory() as directory:
            monitor=MonitoringService(Path(directory)/'collector.sqlite',lambda _: {},lambda **_: {},lambda *_: None)
            with patch.object(dashboard_server,'DB_PATH',monitor.db_path):dashboard_server.init_db()
            monitor.init_schema();monitor.apply_edge_configuration(cloud_api.edge_configuration_payload({'customer_id':cid,'site_id':sid}))
            saved=[d for r in monitor.configuration(cid) for d in r['devices']]
            self.assertEqual([(d['id'],d['host']) for d in saved],[(self.device_id,'10.12.0.140')])
