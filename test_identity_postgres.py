import json,os,unittest
from unittest.mock import patch
from bind_device_identity import bind
import cloud_api
from test_device_deletion_postgres import seed_customer

@unittest.skipUnless(os.environ.get('DCP_DEVICE_DELETE_TEST_DATABASE_URL'),'isolated PostgreSQL required')
class IdentityCloud(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn=os.environ['DCP_DEVICE_DELETE_TEST_DATABASE_URL']
        if not dsn.endswith('/devicedeleteqa'):raise RuntimeError('isolated QA database required')
        cls.env=patch.dict(os.environ,DATABASE_URL=dsn);cls.env.start();cloud_api.initialize_schema()
    @classmethod
    def tearDownClass(cls):cls.env.stop()
    def setUp(self):
        f=seed_customer(self.id());self.customer=f['user']['customer_id'];self.site=f['payload']['site_id'];self.device=f['device_id']
    def version(self):
        with cloud_api.connect() as db:return db.execute('SELECT config_version FROM sites WHERE id=%s',(self.site,)).fetchone()[0]
    def apply(self,mac='02:00:00:00:00:03',apply=True):
        with cloud_api.connect() as db:return bind(db,self.customer,self.site,self.device,mac,'QA reviewed physical fixture',apply)
    def test_preview_is_readonly_and_apply_scoped_audited(self):
        before=self.version();self.assertFalse(self.apply(apply=False)['applied']);self.assertEqual(self.version(),before)
        self.apply();self.assertEqual(self.version(),before+1)
        rooms=cloud_api.edge_configuration_payload(dict(customer_id=self.customer,site_id=self.site))['rooms']
        device=next(d for r in rooms for d in r['devices'] if d['id']==self.device)
        self.assertTrue(device['identity_required']);self.assertEqual(device['expected_mac'],'02:00:00:00:00:03')
        with cloud_api.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM configuration_audit_events WHERE customer_id=%s AND action='device.identity_bound'",(self.customer,)).fetchone()[0],1)
    def test_other_tenant_or_site_cannot_bind(self):
        other=seed_customer('other')
        with cloud_api.connect() as db:
            with self.assertRaises(ValueError):bind(db,other['user']['customer_id'],self.site,self.device,'02:00:00:00:00:03','QA',True)
        with cloud_api.connect() as db:
            with self.assertRaises(ValueError):bind(db,self.customer,self.customer+'-other',self.device,'02:00:00:00:00:03','QA',True)
    def test_unbound_policy_is_explicit_and_preserves_historical_tables(self):
        with cloud_api.connect() as db:before=db.execute('SELECT count(*) FROM readings WHERE customer_id=%s',(self.customer,)).fetchone()
        self.apply(mac=None)
        with cloud_api.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM readings WHERE customer_id=%s',(self.customer,)).fetchone(),before)
            self.assertEqual(db.execute('SELECT expected_mac,required FROM device_identity_bindings WHERE device_id=%s',(self.device,)).fetchone(),(None,True))
    def test_binding_does_not_leak_to_other_collector_configuration(self):
        self.apply()
        rooms=cloud_api.edge_configuration_payload(dict(customer_id=self.customer,site_id=self.customer+'-other'))['rooms']
        self.assertFalse(any('expected_mac' in d for r in rooms for d in r['devices']))
