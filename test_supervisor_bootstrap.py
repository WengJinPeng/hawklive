import base64
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import supervisor_bootstrap as b
from update_protocol import atomic_json, canonical

class Crash(BaseException):pass

class Backend:
    def __init__(self,scope):self.identity=scope;self.stops=0;self.starts=0;self.good=True;self.registered=False
    def scope(self):return self.identity
    def preflight(self):pass
    def register_recovery(self,helper):self.registered=True
    def verify_recovery(self,helper):
        if not self.registered:raise ValueError('Recovery absent')
    def launch_recovery(self):pass
    def stop_and_wait(self):self.stops+=1
    def start(self):self.starts+=1
    def trial(self,*args):return self.good
    def recovered(self,version):return True

class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'app';self.root.mkdir()
        self.data=Path(self.temp.name)/'data';self.data.mkdir()
        (self.root/b.SUPERVISOR).write_bytes(b'MZ-old-supervisor')
        (self.root/b.WORKER).write_bytes(b'MZ-committed-bridge')
        (self.data/'collector.sqlite3').write_bytes(b'unchanged database and WAL sentinel')
        (self.data/'collector.sqlite3-wal').write_bytes(b'new samples')
        atomic_json(self.root/'collector-installed.json',{'version':'0.6.15'})
        atomic_json(self.data/'collector-update-status.json',{'state':'updated','current_version':'0.6.15'})
        self.scope=dict(customer_id='a',site_id='s',instance_id='i')
        self.backend=Backend(self.scope)
        self.key=Ed25519PrivateKey.generate();self.keys={'qa':self.key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()}
        self.plan=dict(purpose='supervisor-bootstrap-v1',id='a'*32,scope=self.scope,issued_at=int(time.time())-1,expires_at=int(time.time())+600,
                       previous_sha256=b.digest(self.root/b.SUPERVISOR),target_sha256=b.digest(self.root/b.WORKER),worker_sha256=b.digest(self.root/b.WORKER),trial_seconds=600)
        self.m=b.Migration(self.root,self.data,self.backend,keys=self.keys)
    def envelope(self):return dict(plan=self.plan,key_id='qa',signature=base64.b64encode(self.key.sign(canonical(self.plan))).decode())
    def assert_data(self):
        self.assertEqual((self.data/'collector.sqlite3').read_bytes(),b'unchanged database and WAL sentinel')
        self.assertEqual((self.data/'collector.sqlite3-wal').read_bytes(),b'new samples')
    def test_commit_preserves_worker_database_and_blocks_repeat(self):
        self.m.prepare(self.envelope());self.assertEqual(self.backend.stops,0)
        self.assertEqual(self.m.run(),'committed');self.assertEqual(b.digest(self.root/b.SUPERVISOR),self.plan['target_sha256'])
        self.assertEqual(self.m.run(),'committed');self.assertEqual(self.backend.stops,1)
        with self.assertRaises(ValueError):self.m.prepare(self.envelope())
        self.assert_data()
    def test_failed_trial_restores_supervisor_without_restoring_database(self):
        self.m.prepare(self.envelope());self.backend.good=False
        self.assertEqual(self.m.run(),'rolled_back');self.assertEqual(b.digest(self.root/b.SUPERVISOR),self.plan['previous_sha256']);self.assert_data()
    def test_crash_at_each_mutating_state_recovers_offline(self):
        for phase in ('stopping','stopped','switching','trial','rolling_back'):
            with self.subTest(phase=phase):
                if self.m.path.exists():self.m.path.unlink()
                (self.root/b.SUPERVISOR).write_bytes(b'MZ-old-supervisor')
                self.m.prepare(self.envelope())
                original=self.m.save
                def crash(r,p):
                    original(r,p)
                    if p==phase:raise Crash()
                self.backend.good=False
                with patch.object(self.m,'save',side_effect=crash):
                    with self.assertRaises(Crash):self.m.run()
                # Expiry must never stop recovery of an already armed transaction.
                self.assertEqual(self.m.run(),'rolled_back')
                self.assertEqual(b.digest(self.root/b.SUPERVISOR),self.plan['previous_sha256']);self.assert_data()
    def test_wrong_customer_instance_and_bad_signature_make_no_changes(self):
        for field in self.scope:
            scope=dict(self.scope);scope[field]='other';self.backend.identity=scope
            with self.assertRaises(ValueError):self.m.prepare(self.envelope())
            self.assertFalse(self.m.path.exists());self.assertEqual(self.backend.stops,0)
        self.backend.identity=self.scope
        bad=self.envelope();bad['plan']=dict(self.plan,trial_seconds=1)
        with self.assertRaises(Exception):self.m.prepare(bad)
        self.assertFalse(self.m.path.exists())
    def test_legacy_pending_transaction_prevents_preparation(self):
        atomic_json(self.root/'collector-update-transaction.json',{'target':'0.6.15'})
        with self.assertRaises(ValueError):self.m.prepare(self.envelope())
        self.assertFalse(self.backend.registered)
    def test_damaged_backup_does_not_stop_running_process(self):
        self.m.prepare(self.envelope());r=b.read_json(self.m.path);self.m.save(r,'trial')
        (self.root/'bootstrap-previous.exe').write_bytes(b'bad')
        with self.assertRaises(ValueError):self.m.run()
        self.assertEqual(self.backend.stops,0)
    def test_recovery_registration_failure_leaves_main_untouched(self):
        with patch.object(self.backend,'register_recovery',side_effect=OSError):
            with self.assertRaises(OSError):self.m.prepare(self.envelope())
        self.assertEqual(self.m.run(),'prepared');self.assertEqual(self.backend.stops,0)
    def test_expired_armed_plan_does_not_stop_main(self):
        self.m.prepare(self.envelope())
        with patch('supervisor_bootstrap.time.time',return_value=self.plan['expires_at']+1):
            with self.assertRaises(ValueError):self.m.run()
        self.assertEqual(self.backend.stops,0)

if __name__=='__main__':unittest.main()
