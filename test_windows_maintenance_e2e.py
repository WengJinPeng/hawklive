"""Real frozen EXE checks on a disposable Windows runner and loopback API only."""
from __future__ import annotations

import ast
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from collector_settings import CollectorSettings, save_settings
from collector_updater import EXE_NAME, Supervisor
from cloud_sync import COLLECTOR_VERSION
import maintenance_protocol as protocol
from test_collector_maintenance import reading


class LoopbackMaintenance:
    def __init__(self, private, readonly):
        self.private=private;self.readonly=readonly
        self.snapshot=None;self.arrived=threading.Event();self.release=threading.Event();self.done=threading.Event()
        self.normal_reading=threading.Event();self.normal_alarm=threading.Event()
        self.task=None;self.state='pending';self.cloud_uuids=set();self.executes=0;self.acks=0;self.failures=[]
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def reply(self,payload,status=200):
                body=json.dumps(payload).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers()
                try:self.wfile.write(body)
                except OSError:pass
            def do_GET(self):
                if self.path.startswith('/api/v1/edge/config'):
                    return self.reply({'ok':True,'data':{'customer_id':'maintenance-fixture','site_id':'maintenance-site','site_name':'Loopback QA','revision':1,'rooms':[]}})
                if self.path.startswith('/api/v1/edge/quarantine/tasks'):
                    jobs=[]
                    if not owner.readonly and owner.task and owner.state!='completed':
                        if owner.state=='accepted':jobs=[{'state':'accepted','receipt':owner.receipt()}]
                        else:jobs=[{'state':'pending','task':protocol.signed(owner.task,owner.private)}]
                    return self.reply({'ok':True,'data':jobs})
                return self.reply({'ok':True,'data':None,'available':False})
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
                if self.path.endswith('/quarantine/snapshot'):
                    if owner.snapshot is None and payload['items']:
                        owner.snapshot=payload
                        item=payload['items'][0];now=int(time.time())
                        owner.task={'protocol':1,'kind':'request','action':'retry_reading','task_id':str(uuid4()),
                            'customer_id':'maintenance-fixture','site_id':'maintenance-site',
                            'instance_id':self.headers['X-DCP-Collector-Instance'],
                            'record_uuid':item['record_id'].replace('-',''),'raw_sha256':item['raw_sha256'],
                            'issued_at':now,'expires_at':now+protocol.TASK_TTL}
                        owner.arrived.set()
                        if owner.readonly:owner.release.wait(15)
                    return self.reply({'ok':True})
                if self.path.endswith('/execute'):
                    owner.executes+=1
                    if not owner.task or protocol.raw_digest(payload['raw_text'])!=owner.task['raw_sha256']:
                        return self.reply({'detail':'Original differs'},409)
                    owner.cloud_uuids.add(owner.task['record_uuid']);owner.state='accepted'
                    # Simulate a lost reply after the original row is already accepted.
                    return self.reply({'detail':'Synthetic reply failure'},503)
                if self.path.endswith('/ack'):
                    owner.acks+=1
                    if owner.acks==1:return self.reply({'detail':'Synthetic acknowledgement failure'},503)
                    owner.state='completed';owner.done.set();return self.reply({'ok':True})
                if self.path.endswith('/fail'):
                    owner.failures.append(payload);return self.reply({'ok':True})
                if self.path.endswith('/readings/batch'):
                    if payload['readings']:owner.normal_reading.set()
                    return self.reply({'ok':True,'accepted':[row['record_uuid'] for row in payload['readings']]})
                if self.path.endswith('/alarms/batch'):
                    if payload['events']:owner.normal_alarm.set()
                    return self.reply({'ok':True,'accepted':[row['event_uuid'] for row in payload['events']]})
                if self.path.endswith('/diagnostics/batch'):
                    return self.reply({'ok':True,'accepted':[row['record_uuid'] for row in payload['logs']]})
                return self.reply({'ok':True,'data':{'desired_config_version':1}})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def receipt(self):
        now=int(time.time())
        return protocol.signed({**self.task,'kind':'receipt','issued_at':now,'expires_at':now+protocol.TASK_TTL,'accepted_at':now},self.private)

    def close(self):
        self.release.set();self.server.shutdown();self.server.server_close();self.thread.join(3)


@unittest.skipUnless(os.name=='nt' and os.environ.get('DCP_UPDATER_ISOLATED_WINDOWS_QA')=='1','Disposable Windows runner and frozen artifact required')
class NativeMaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shipping=Path(os.environ['DCP_UPDATE_TEST_EXE']).resolve()
        cls.fixture=tempfile.TemporaryDirectory(prefix='maintenance-native-build-')
        cls.private=Ed25519PrivateKey.generate()
        public=cls.private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex()
        source=Path(cls.fixture.name)/'src'
        shutil.copytree(Path(__file__).parent,source,ignore=shutil.ignore_patterns('.git','.venv','release','audit','build','dist','__pycache__','*.sqlite3*'))
        path=source/'maintenance_protocol.py';original=path.read_text(encoding='utf-8')
        node=next(n for n in ast.parse(original).body if isinstance(n,ast.AnnAssign) and isinstance(n.target,ast.Name) and n.target.id=='TRUSTED_KEYS')
        lines=original.splitlines(keepends=True)
        test_source=''.join(lines[:node.lineno-1])+f'TRUSTED_KEYS: dict[str,str] = {{{protocol.KEY_ID!r}:{public!r}}}\n'+''.join(lines[node.end_lineno:])
        others=lambda text:[ast.dump(n) for n in ast.parse(text).body if not(isinstance(n,ast.AnnAssign) and isinstance(n.target,ast.Name) and n.target.id=='TRUSTED_KEYS')]
        if others(original)!=others(test_source):raise AssertionError('Only the ephemeral QA trust root may differ')
        path.write_text(test_source,encoding='utf-8')
        result=subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile','--name','HawkHive-Maintenance-QA',
            '--collect-data','certifi','--add-data',str(source/'public')+';public','--distpath',str(Path(cls.fixture.name)/'bin'),
            '--workpath',str(Path(cls.fixture.name)/'build'),'--specpath',str(Path(cls.fixture.name)/'spec'),str(source/'windows_launcher.py')],cwd=source,capture_output=True,text=True,timeout=360)
        if result.returncode:raise AssertionError('Isolated native QA build failed: '+(result.stdout+result.stderr)[-3500:])
        cls.qa_exe=Path(cls.fixture.name)/'bin/HawkHive-Maintenance-QA.exe'

    @classmethod
    def tearDownClass(cls):cls.fixture.cleanup()

    def setup_worker(self, readonly):
        self.temp=tempfile.TemporaryDirectory(prefix='maintenance-native-data-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'app').mkdir();(self.root/'data').mkdir()
        shutil.copy2(self.shipping if readonly else self.qa_exe,self.root/'app'/EXE_NAME)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        self.supervisor=Supervisor(self.root/'app',self.root/'data',port)
        self.addCleanup(lambda:self.supervisor.stop_worker(force=True) if self.supervisor.child else None)
        self.supervisor.start_worker()
        with patch('collector_updater.HEALTH_STABLE_SECONDS',1):self.assertTrue(self.supervisor.healthy_trial(COLLECTOR_VERSION))
        self.assertTrue(self.supervisor.stop_worker())
        self.database=self.root/'data/dashboard_data.sqlite3'
        self.original=reading(customer_id='maintenance-fixture',site_id='maintenance-site',cleanroom_id='maintenance-room',device_id='maintenance-device')
        self.insert_reading(self.original,quarantined=True)
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO alarm_events(id,customer_id,site_id,cleanroom_id,device_id,source,metric,started_at,ended_at,limit_description,synced_at) VALUES('protected-fixture-alarm','maintenance-fixture','maintenance-site','maintenance-room','maintenance-device','device','protected',1000,1001,'retained',1001)")
        self.original_row=self.local_row(self.original['record_uuid'])
        self.loopback=LoopbackMaintenance(self.private,readonly);self.addCleanup(self.loopback.close)
        self.settings=self.root/'data/collector_settings.json'
        save_settings(self.settings,CollectorSettings('http://127.0.0.1:'+str(self.loopback.server.server_port),'maintenance-site','synthetic-loopback-token'))
        self.settings_digest=hashlib.sha256(self.settings.read_bytes()).hexdigest()
        self.supervisor.start_worker()
        with patch('collector_updater.HEALTH_STABLE_SECONDS',1):self.assertTrue(self.supervisor.healthy_trial(COLLECTOR_VERSION))
        self.assertTrue(self.loopback.arrived.wait(60),'Frozen artifact must report its existing quarantined row')

    def insert_reading(self,row,quarantined):
        fields=list(protocol.SOURCE_FIELDS)+['host','slave','alarm_raw','alarms_json','sync_attempts','sync_error','sync_quarantined_at']
        values=[row[field] for field in protocol.SOURCE_FIELDS]+['127.0.0.1',1,0,'[]',5 if quarantined else 0,'HTTP Error 422' if quarantined else None,time.time() if quarantined else None]
        with sqlite3.connect(self.database,timeout=2) as db:db.execute('INSERT INTO readings('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',values)

    def local_row(self,record_uuid):
        with sqlite3.connect(self.database,timeout=2) as db:
            db.row_factory=sqlite3.Row
            return dict(db.execute('SELECT * FROM readings WHERE record_uuid=?',(record_uuid,)).fetchone())

    def assert_preserved(self):
        self.assertEqual(hashlib.sha256(self.settings.read_bytes()).hexdigest(),self.settings_digest)
        with sqlite3.connect(self.database) as db:self.assertEqual(db.execute("SELECT synced_at,ended_at FROM alarm_events WHERE id='protected-fixture-alarm'").fetchone(),(1001,1001))
        self.assertTrue(self.supervisor.health()['monitor_running'])

    def test_shipping_exe_reports_details_while_history_and_alarm_uploads_continue(self):
        self.setup_worker(readonly=True)
        item=self.loopback.snapshot['items'][0]
        self.assertEqual(json.loads(item['raw_text']),self.original)
        # Hold the maintenance HTTP request while adding independent normal work.
        normal=reading(customer_id='maintenance-fixture',site_id='maintenance-site',cleanroom_id='maintenance-room',device_id='maintenance-device',timestamp=2000)
        self.insert_reading(normal,quarantined=False)
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO alarm_events(id,customer_id,site_id,cleanroom_id,device_id,source,metric,started_at,ended_at,limit_description) VALUES(?,?,?,?,?,'device','fixture',2000,2001,'synthetic')",(str(uuid4()),'maintenance-fixture','maintenance-site','maintenance-room','maintenance-device'))
        self.assertTrue(self.loopback.normal_reading.wait(13),'Normal historical uploads must progress during blocked maintenance')
        self.assertTrue(self.loopback.normal_alarm.wait(2),'Normal alarm uploads must progress independently')
        self.assertEqual(self.local_row(self.original['record_uuid']),self.original_row)
        self.assert_preserved();self.loopback.release.set()

    def test_frozen_signed_retry_recovers_lost_receipts_and_retains_backup(self):
        self.setup_worker(readonly=False)
        self.assertTrue(self.loopback.done.wait(80),'Frozen signed retry and acknowledgement recovery must finish')
        self.assertFalse(self.loopback.failures,self.loopback.failures)
        self.assertEqual(self.loopback.executes,1);self.assertEqual(self.loopback.acks,2)
        self.assertEqual(self.loopback.cloud_uuids,{self.original['record_uuid']})
        after=self.local_row(self.original['record_uuid'])
        self.assertIsNone(after['sync_quarantined_at']);self.assertIsNotNone(after['synced_at'])
        for key in self.original_row.keys()-{'synced_at','sync_error','sync_quarantined_at'}:self.assertEqual(after[key],self.original_row[key])
        with sqlite3.connect(self.database) as db:
            backup=db.execute('SELECT original_row_json,state FROM maintenance_reading_backups').fetchone()
            self.assertEqual(json.loads(backup[0]),self.original_row);self.assertEqual(backup[1],'completed')
        self.assert_preserved()


if __name__=='__main__':unittest.main()
