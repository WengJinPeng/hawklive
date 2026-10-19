from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from collector_diagnostics import log_scope
from device_link_diagnostics import DeviceLinkDiagnostics, network_snapshot
from dcp8001_collector import Dcp8001TcpClient
from test_dcp8001_collector import FakeSocket


class LinkEvidenceTests(unittest.TestCase):
    def device(self, host='10.12.0.132'):
        return {'id': 'device-7', 'host': host}

    def service(self, snapshot=lambda host: {'state': 'observed'}):
        self.logs = []
        svc = DeviceLinkDiagnostics(lambda level, event, message:
            self.logs.append((log_scope.get(), event, json.loads(message))), snapshot=snapshot)
        self.addCleanup(svc.close)
        return svc

    def drain(self, svc):
        deadline = time.monotonic() + 2
        while svc.jobs.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(svc.jobs.unfinished_tasks, 0)

    def test_slow_network_query_does_not_block_submit_and_preserves_tenant(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def snapshot(host):
            entered.set(); release.wait(2)
            return {'state': 'observed'}
        svc = self.service(snapshot)
        begin = time.monotonic()
        svc.submit('customer-a', 'site-a', self.device(), {'stage': 'connect'})
        self.assertLess(time.monotonic() - begin, .2)
        self.assertTrue(entered.wait(1))
        begin = time.monotonic()
        for _ in range(30): svc.submit('customer-b', 'site-b', self.device(), {'stage': 'connect'})
        self.assertLess(time.monotonic() - begin, .2)
        release.set(); self.drain(svc)
        self.assertEqual({row[0] for row in self.logs}, {('customer-a', 'site-a'), ('customer-b', 'site-b')})

    def test_rate_limit_keeps_error_transitions_and_excludes_other_devices(self):
        svc = self.service()
        for _ in range(3):
            for evidence in (
                {'stage': 'response_header', 'error_type': 'ValueError', 'frames': [{'header':'00000000070103'}]},
                {'stage': 'response_header', 'error_type': 'ValueError', 'frames': [{'header':'41542b574d4f44'}]},
                {'stage': 'connect'},
            ): svc.submit('c', 's', self.device(), evidence)
        svc.submit('c', 's', self.device('10.12.0.140'), {'stage': 'connect'})
        self.drain(svc)
        self.assertEqual(sum(r[2]['part']=='protocol' for r in self.logs), 3)
        self.assertEqual(len(svc.last), 3)

    def test_queue_and_close_are_bounded_even_if_query_stalls(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def snapshot(host): entered.set(); release.wait(2); return {'state': 'observed'}
        svc = self.service(snapshot)
        svc.submit('c', 's', self.device(), {})
        self.assertTrue(entered.wait(1))
        for n in range(50):svc.submit(str(n), 's', self.device(), {})
        self.assertLessEqual(svc.jobs.qsize(), 8)
        self.assertLessEqual(len(svc.last), 9)
        begin=time.monotonic();svc.close()
        self.assertLess(time.monotonic()-begin,.1)
        release.set()
        svc.thread.join(1)
        self.assertFalse(svc.thread.is_alive())
        self.assertEqual(self.logs, [])

    def test_query_exception_does_not_kill_worker_or_emit_secrets(self):
        calls=[]
        def snapshot(host):
            calls.append(host)
            if len(calls)==1:raise RuntimeError('password=secret')
            return {'state':'observed'}
        svc=self.service(snapshot)
        svc.submit('c','s',self.device(),{})
        svc.submit('c','s',self.device(),{'stage':'connect'})
        self.drain(svc)
        self.assertEqual(len(calls),2)
        self.assertEqual(sum(r[2]['part']=='protocol' for r in self.logs),2)
        self.assertNotIn('secret',str(self.logs))

    def test_json_events_remain_within_existing_upload_limit(self):
        svc=self.service(lambda host:{'tcp':[{'State':'Established'}]*500})
        svc.submit('c','s',self.device(),{'stage':'connect'})
        self.drain(svc)
        self.assertTrue(all(len(json.dumps(row[2]))<2000 for row in self.logs))
        self.assertTrue(any(row[2]['data']=={'state':'size_limit'} for row in self.logs))

    def test_windows_query_has_timeout_no_active_probe_and_no_error_output(self):
        with patch('device_link_diagnostics.os.name','nt'), patch('device_link_diagnostics.subprocess.run') as run:
            run.side_effect=subprocess.TimeoutExpired('powershell',15,output=b'password=secret')
            value=network_snapshot('10.12.0.115')
            self.assertEqual(value,{'state':'unavailable','error_type':'TimeoutExpired'})
            args,kw=run.call_args
            self.assertEqual(kw['timeout'],15)
            import base64
            script=base64.b64decode(args[0][-1]).decode('utf-16le')
            for text in ('Test-NetConnection','Invoke-','Set-Net','Get-Process','CommandLine'):
                self.assertNotIn(text,script)
            self.assertIn('Get-NetNeighbor',script)
            self.assertIn('Get-NetTCPConnection',script)

    def test_snapshot_captures_transaction_and_partial_read_without_body(self):
        client=object.__new__(Dcp8001TcpClient)
        client.slave=1;client.timeout=1;client.connect_settle=0;client.transaction_id=0;client._first_request=False
        client.sock=FakeSocket(bytes.fromhex('000100000007010304c1'))
        with self.assertRaises(ConnectionError): client.read_holding_registers(0,2)
        evidence=client.diagnostic_snapshot()
        self.assertEqual(evidence['frames'],[{'tx':1,'register':0,'rx':1,'header':'00010000000701','headers_seen':1}])
        self.assertEqual((evidence['expected_bytes'],evidence['received_bytes']),(6,3))
        self.assertNotIn('0304c1',json.dumps(evidence))

    def test_evidence_uses_existing_persistent_queue_and_acknowledged_upload(self):
        import tempfile
        from pathlib import Path
        from collector_diagnostics import enqueue
        from cloud_sync import CloudSyncService
        with tempfile.TemporaryDirectory() as directory:
            sync=CloudSyncService(Path(directory)/'queue.sqlite3', 'https://example.invalid',
                                  'test-token', 'site-a', lambda *args:None)
            sync.customer_id='customer-a'
            sync._last_diagnostic_snapshot=time.monotonic()
            def emit(level,event,message):
                customer,site=log_scope.get()
                with sync.connect() as db:
                    enqueue(db,customer,site,sync.instance_id,level,event,message)
            svc=DeviceLinkDiagnostics(emit,snapshot=lambda host:{'state':'observed'})
            try:
                svc.submit('customer-a','site-a',self.device(),{'stage':'connect'})
                self.drain(svc)
                bodies=[]
                def request(req):
                    body=json.loads(req.data);bodies.append(body)
                    return {'ok':True,'accepted':[r['record_uuid'] for r in body['logs']]}
                sync._request_json=request
                self.assertEqual(sync.sync_diagnostics_once(),2)
                self.assertTrue(all(r['event']=='device_link_evidence' for r in bodies[0]['logs']))
                self.assertTrue(all(json.loads(r['message'])['host']=='10.12.0.132' for r in bodies[0]['logs']))
                with sync.connect() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM diagnostic_outbox').fetchone()[0],0)
            finally:
                svc.close();svc.thread.join(1)

    @unittest.skipUnless(os.name=='nt','requires native Windows network cmdlets')
    def test_native_windows_read_only_queries(self):
        value=network_snapshot('127.0.0.1')
        self.assertEqual(value['state'],'observed',value)
        self.assertIn('route',value)
        self.assertTrue(value['route'])

    def test_disabled_hosts_start_no_thread(self):
        svc=DeviceLinkDiagnostics(lambda *args:None,hosts=[])
        svc.submit('c','s',self.device(),{})
        self.assertIsNone(svc.thread)


if __name__=='__main__':unittest.main()
