"""Offline fault injection; no production endpoints or installed directories."""
import json
import unittest
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import test_collector_updater as fixtures
import collector_updater as u
import collector_update_api as api
import update_protocol as p


class SafeUpdateTests(unittest.TestCase):
    setUp = fixtures.UpdaterTests.setUp
    tearDown = fixtures.UpdaterTests.tearDown
    supervisor = fixtures.UpdaterTests.supervisor
    run_check = fixtures.UpdaterTests.run_check

    def test_business_trial_rejects_stalled_workers_but_accepts_all_devices_offline(self):
        s=self.supervisor();s.child.poll.return_value=None
        clock=[0.0]
        def sleep(seconds):clock[0]+=seconds
        def health():
            return dict(version='0.6.1',health_contract=1,monitor_running=True,storage_state='healthy',
                        scheduler={'tick':int(clock[0])+1,'age_seconds':0,'oldest_attempt_seconds':clock[0],'internal_failures':0},
                        online_devices=0,pending_uploads=1000,cloud_state='offline')
        with patch.object(u.time,'monotonic',side_effect=lambda:clock[0]),patch.object(u.time,'sleep',side_effect=sleep),patch.object(u,'PROBATION_SECONDS',40):
            s.health=Mock(side_effect=health)
            self.assertTrue(s.healthy_trial('0.6.1',business=True))
            clock[0]=0
            frozen=health();s.health=Mock(return_value=frozen)
            self.assertFalse(s.healthy_trial('0.6.1',business=True))
            clock[0]=0
            frozen['scheduler']['oldest_attempt_seconds']=181
            self.assertFalse(s.healthy_trial('0.6.1',business=True))

    def test_invalid_scope_and_missing_contract_never_stop_or_download(self):
        s=self.supervisor()
        for scope in ({}, {'customer_id':'other','site_id':'qa-site','instance_id':'qa-instance'}):
            with self.assertRaises(ValueError):p.verify_scope(self.release,scope)
        with self.assertRaises(ValueError):p.verify_scope({**self.release,'health_contract':0},self.release['scope'])
        s.health=Mock(return_value={'update_scope':{'site_id':'other'}})
        self.run_check(s);s.stop_worker.assert_not_called()

    def test_corrupt_backup_and_journal_fail_closed(self):
        tx=u.UpdateTransaction(self.root);tx.staged.write_bytes(self.content);tx.switch(self.release,'0.6.0')
        tx.previous.write_bytes(b'MZ-corrupt')
        with self.assertRaises(RuntimeError):tx.recover()
        self.assertTrue(tx.journal.exists())
        tx.journal.write_text('{')
        with self.assertRaises(RuntimeError):tx.recover()
        self.assertTrue(tx.journal.exists())

    def test_failed_stop_leaves_transaction_for_next_boot(self):
        s=self.supervisor();s.healthy_trial=Mock(return_value=False)
        s.stop_worker=Mock(side_effect=[True,False])
        with self.assertRaises(RuntimeError):self.run_check(s)
        self.assertTrue(s.transaction.journal.exists())
        self.assertEqual(s.start_worker.call_count,1)

    def test_scoped_release_never_falls_back_to_global_and_rejects_other_tenant(self):
        identity=dict(self.release['scope'])
        app=FastAPI();api.register_update_routes(app,lambda:identity)
        with patch.object(api,'release_directory',return_value=self.root),patch.dict(p.TRUSTED_KEYS,self.keys),TestClient(app) as http:
            p.atomic_json(self.root/'stable.json',self.envelope)
            (self.root/(self.release['sha256']+'.exe')).write_bytes(self.content)
            self.assertFalse(http.get('/api/v1/collector-updates/scoped/stable').json()['available'])
            path=self.root/'scoped'/api.scope_filename(identity);p.atomic_json(path,self.envelope)
            self.assertEqual(http.get('/api/v1/collector-updates/scoped/stable').status_code,200)
            identity['customer_id']='other'
            self.assertFalse(http.get('/api/v1/collector-updates/scoped/stable').json()['available'])
            p.atomic_json(self.root/'scoped'/api.scope_filename(identity),self.envelope)
            self.assertEqual(http.get('/api/v1/collector-updates/scoped/stable').status_code,503)

    def test_real_process_crash_after_switch_restores_original_without_data_changes(self):
        import subprocess,sys
        sentinel=self.root/'collector.sqlite3';sentinel.write_bytes(b'customer history and bindings')
        script='''import os,sys
from pathlib import Path
from collector_updater import UpdateTransaction
from update_protocol import read_json
root=Path(sys.argv[1]);tx=UpdateTransaction(root)
tx.switch(read_json(root/'release.json'),'0.6.0')
os._exit(71)
'''
        p.atomic_json(self.root/'release.json',self.release)
        tx=u.UpdateTransaction(self.root);tx.staged.write_bytes(self.content)
        child=subprocess.run([sys.executable,'-c',script,str(self.root)],timeout=10)
        self.assertEqual(child.returncode,71)
        self.assertEqual(self.exe.read_bytes(),self.content)
        tx.recover()
        self.assertEqual(self.exe.read_bytes(),b'MZ-known-good')
        self.assertEqual(sentinel.read_bytes(),b'customer history and bindings')

    def test_corrupt_status_keeps_auto_install_disabled_across_restart(self):
        (self.root/'collector-update-status.json').write_text('{')
        first=u.Supervisor(self.root,self.root,8787);first.report('starting')
        second=u.Supervisor(self.root,self.root,8787)
        self.assertFalse(second.update_state_readable)

    def test_cloud_scope_authentication_is_readonly_and_requires_active_instance(self):
        from contextlib import contextmanager
        import cloud_api
        from fastapi import HTTPException
        calls=[]
        class DB:
            def execute(self,sql,params):
                calls.append((sql,params));return self
            def fetchone(self):
                return ('qa','qa-site') if calls[-1][1][1]=='qa-instance' else None
        @contextmanager
        def connection():yield DB()
        with patch.object(cloud_api,'connect',connection):
            self.assertEqual(cloud_api.update_identity('Bearer fixture','qa-instance'),self.release['scope'])
            with self.assertRaises(HTTPException):cloud_api.update_identity('Bearer fixture','other-instance')
            with self.assertRaises(HTTPException):cloud_api.update_identity(None,'qa-instance')
        self.assertTrue(all(sql.lstrip().startswith('SELECT') for sql,_ in calls))
        self.assertTrue(all('s.customer_id=t.customer_id' in sql for sql,_ in calls))
        self.assertTrue(all('active_collector_instance=%s' in sql for sql,_ in calls))

    def test_real_scheduler_reports_live_progress_and_outstanding_attempt_age(self):
        import threading,time
        from monitoring_service import MonitoringService
        started=threading.Event();release=threading.Event()
        service=MonitoringService(self.root/'unused.sqlite3',Mock(),Mock(),Mock())
        service.configuration=Mock(return_value=[{'devices':[dict(id='qa',host='10.0.0.1',tcpPort=502,slave=1,enabled=True)]}])
        service._watch_network=Mock()
        def poll(*args):started.set();release.wait(3)
        service._poll_device=poll
        try:
            service.start();self.assertTrue(started.wait(1))
            deadline=time.monotonic()+2
            while service.scheduler_health()['tick']<2 and time.monotonic()<deadline:time.sleep(.02)
            health=service.scheduler_health()
            self.assertGreaterEqual(health['tick'],2)
            self.assertLess(health['age_seconds'],2)
            self.assertGreater(health['oldest_attempt_seconds'],.1)
        finally:
            release.set();service.stop()

    def test_scoped_manifest_is_rejected_by_legacy_channel(self):
        self.assertEqual(self.release['protocol'],2)  # Legacy verifier accepts only 1.
        p.atomic_json(self.root/'stable.json',self.envelope)
        (self.root/(self.release['sha256']+'.exe')).write_bytes(self.content)
        with patch.object(api,'release_directory',return_value=self.root),patch.dict(p.TRUSTED_KEYS,self.keys):
            with self.assertRaises(ValueError):api.current_release()

    def test_late_start_gets_full_trial_and_internal_failures_never_commit(self):
        s=self.supervisor();s.child.poll.return_value=None;clock=[0.0]
        def sleep(seconds):clock[0]+=seconds
        def health():
            if clock[0]<118:return {}
            return dict(version='0.6.1',health_contract=1,monitor_running=True,storage_state='healthy',
                        scheduler={'tick':int(clock[0])+1,'age_seconds':0,'oldest_attempt_seconds':0,'internal_failures':0})
        with patch.object(u.time,'monotonic',side_effect=lambda:clock[0]),patch.object(u.time,'sleep',side_effect=sleep),patch.object(u,'PROBATION_SECONDS',40):
            s.health=Mock(side_effect=health);self.assertTrue(s.healthy_trial('0.6.1',business=True))
            self.assertGreaterEqual(clock[0],158)
            unhealthy=health();unhealthy['scheduler']['internal_failures']=1
            s.health=Mock(return_value=unhealthy)
            self.assertFalse(s.healthy_trial('0.6.1',business=True))

    def test_post_commit_status_failure_does_not_roll_back_or_stop_new_worker(self):
        s=self.supervisor();s.healthy_trial=Mock(return_value=True)
        report=s.report
        def faulty(state,**values):
            if state=='updated':raise PermissionError('fixture status lock')
            return report(state,**values)
        s.report=faulty
        self.run_check(s)
        self.assertEqual(s.current,'0.6.1');self.assertEqual(self.exe.read_bytes(),self.content)
        self.assertFalse(s.transaction.journal.exists())
        self.assertEqual(s.stop_worker.call_count,1)
        self.assertFalse(s.update_state_readable)

    def test_programming_error_is_internal_but_bad_network_frame_is_not(self):
        from test_device_recovery import DeviceRecoveryTests
        case=DeviceRecoveryTests();case.setUp()
        try:
            with patch('monitoring_service.Dcp8001TcpClient') as client:
                client.return_value.read_realtime.side_effect=ValueError('bad TCP frame')
                case.service._poll_device(case.room,case.device)
                self.assertEqual(case.service.scheduler_health()['internal_failures'],0)
                case.service._next_poll_at.clear()
                client.return_value.read_realtime.side_effect=TypeError('fixture programming error')
                case.service._poll_device(case.room,case.device)
                self.assertEqual(case.service.scheduler_health()['internal_failures'],1)
        finally:case.doCleanups()
