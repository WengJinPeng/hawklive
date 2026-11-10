"""Disposable Windows: actual legacy signed HTTPS update -> SYSTEM migration.

Only ephemeral release trust roots and the bridge version differ in QA builds.
No production keys, endpoints or customer data are used.
"""
import ast
import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import shutil
import ssl
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ed25519
from cryptography.x509.oid import NameOID
import supervisor_bootstrap as b
import windows_install as wi
from collector_settings import CollectorSettings, save_settings
from update_protocol import canonical, read_json

@unittest.skipUnless(os.name=='nt' and os.environ.get('DCP_UPDATER_ISOLATED_WINDOWS_QA')=='1','Disposable Windows only')
class WindowsBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='bootstrap-build-')
        cls.folder=Path(cls.temp.name)
        cls.private=ed25519.Ed25519PrivateKey.generate()
        public=cls.private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex()
        cls.binaries=[]
        for name,source in [('old',Path('legacy-bootstrap-source')),('bridge',Path.cwd())]:
            target=cls.folder/name
            shutil.copytree(source,target,ignore=shutil.ignore_patterns('.git','legacy-bootstrap-source','.venv','release','audit','build','dist','__pycache__','*.sqlite3*'))
            p=target/'update_protocol.py';original=p.read_text(encoding='utf-8')
            node=next(n for n in ast.parse(original).body if isinstance(n,ast.AnnAssign) and isinstance(n.target,ast.Name) and n.target.id=='TRUSTED_KEYS')
            lines=original.splitlines(keepends=True)
            p.write_text(''.join(lines[:node.lineno-1])+f'TRUSTED_KEYS: dict[str,str] = {{"hawkhive-2026":{public!r}}}\n'+''.join(lines[node.end_lineno:]),encoding='utf-8')
            if name=='bridge':
                p=target/'cloud_sync.py';s=p.read_text(encoding='utf-8');assert 'COLLECTOR_VERSION = "0.6.14"' in s
                p.write_text(s.replace('COLLECTOR_VERSION = "0.6.14"','COLLECTOR_VERSION = "0.6.15"',1),encoding='utf-8')
            result=subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile','--name',name,
                '--collect-data','certifi','--add-data',str(target/'public')+';public','--distpath',str(cls.folder/'bin'),
                '--workpath',str(cls.folder/('build-'+name)),'--specpath',str(cls.folder/('spec-'+name)),str(target/'windows_launcher.py')],
                cwd=target,capture_output=True,text=True,timeout=360)
            if result.returncode:raise AssertionError((result.stdout+result.stderr)[-4000:])
            cls.binaries.append(cls.folder/'bin'/f'{name}.exe')
        # System trust is modified only on a disposable runner and removed by thumbprint.
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Disposable bootstrap loopback QA')])
        cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(minutes=5))
            .not_valid_after(datetime.now(timezone.utc)+timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
            .sign(key,hashes.SHA256()))
        cls.certfile=cls.folder/'cert.pem';cls.certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        cls.keyfile=cls.folder/'key.pem';cls.keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        cls.thumbprint=cert.fingerprint(hashes.SHA1()).hex()
        subprocess.run(['certutil.exe','-addstore','Root',str(cls.certfile)],check=True,capture_output=True,timeout=30)
    @classmethod
    def tearDownClass(cls):
        subprocess.run(['certutil.exe','-delstore','Root',cls.thumbprint],check=True,capture_output=True,timeout=30)
        cls.temp.cleanup()
    def sign(self,kind,payload):
        return {kind:payload,'key_id':'hawkhive-2026','signature':base64.b64encode(self.private.sign(canonical(payload))).decode()}
    def setUp(self):
        self.root,self.data=wi.install_paths()
        self.assertFalse(self.root.exists());self.assertFalse(self.data.exists())
        self.addCleanup(self.cleanup)
        self.scope=dict(customer_id='bootstrap-qa',site_id='bootstrap-site',instance_id='pending')
        self.scope_seen=threading.Event();owner=self
        old,bridge=self.binaries;self.bridge=bridge
        now=int(time.time())
        self.release=self.sign('release',dict(version='0.6.15',platform='windows-x64',protocol=1,size=bridge.stat().st_size,
                                  sha256=b.digest(bridge),issued_at=now,expires_at=now+86400))
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def reply(self,payload):
                raw=json.dumps(payload).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers()
                try:self.wfile.write(raw)
                except OSError:pass
            def do_GET(self):
                if self.path=='/api/v1/collector-updates/stable':return self.reply(owner.release)
                if self.path.endswith('.exe'):
                    raw=owner.bridge.read_bytes();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
                if self.path=='/api/v1/edge/config':
                    return self.reply({'ok':True,'data':dict(customer_id='bootstrap-qa',site_id='bootstrap-site',revision=1,rooms=[])})
                if self.path=='/api/v1/collector-updates/bootstrap/plan':
                    self.assert_identity()
                    return self.reply(owner.sign('plan',dict(purpose='supervisor-bootstrap-v1',id='a'*32,scope=owner.scope,
                        issued_at=now,expires_at=now+86400,previous_sha256=b.digest(old),target_sha256=b.digest(bridge),worker_sha256=b.digest(bridge),trial_seconds=600)))
                if 'quarantine/tasks' in self.path:return self.reply({'ok':True,'data':[]})
                return self.reply({'ok':True,'available':False,'data':None})
            def assert_identity(self):
                if self.headers.get('Authorization')!='Bearer isolated-bootstrap-token':raise AssertionError('Missing authentication')
                owner.scope['instance_id']=self.headers['X-DCP-Collector-Instance'];owner.scope_seen.set()
            def do_POST(self):
                raw=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
                if self.path.endswith('/diagnostics/batch'):return self.reply({'ok':True,'accepted':[x['record_uuid'] for x in raw['logs']]})
                return self.reply({'ok':True,'data':{'desired_config_version':1},'accepted':[]})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(self.certfile,self.keyfile)
        self.server.socket=ctx.wrap_socket(self.server.socket,server_side=True)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.data.mkdir(parents=True)
        save_settings(self.data/'collector_settings.json',CollectorSettings(f'https://127.0.0.1:{self.server.server_port}','bootstrap-site','isolated-bootstrap-token'))
        self.settings=(self.data/'collector_settings.json').read_bytes()
        wi.install_elevated(old,None)
        self.wait(lambda:(self.data/'dashboard_data.sqlite3').exists(),60)
        with closing(sqlite3.connect(self.data/'dashboard_data.sqlite3')) as db,db:
            db.execute("CREATE TABLE bootstrap_preserved_evidence(value TEXT)")
            db.execute("INSERT INTO bootstrap_preserved_evidence VALUES('before update')")
    def wait(self,predicate,seconds):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(2)
        status=read_json(self.root/b.STATE)
        raise AssertionError('Timed out; bootstrap='+str(status.get('phase'))+' error='+str(read_json(self.root/'bootstrap-last-error.json'))+' update='+str(read_json(self.data/'collector-update-status.json')))
    def preserved(self):
        self.assertEqual((self.data/'collector_settings.json').read_bytes(),self.settings)
        with closing(sqlite3.connect(self.data/'dashboard_data.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT value FROM bootstrap_preserved_evidence ORDER BY rowid').fetchall(),[('before update',),('during migration',)])
    def test_a_interrupted_independent_task_recovers_without_cloud(self):
        self.wait(lambda:read_json(self.root/b.STATE).get('phase')=='trial',240)
        with closing(sqlite3.connect(self.data/'dashboard_data.sqlite3')) as db,db:db.execute("INSERT INTO bootstrap_preserved_evidence VALUES('during migration')")
        wi.run_checked(['schtasks.exe','/End','/TN',b.WindowsBackend.TASK])
        self.server.shutdown();self.server.server_close();self.thread.join(5);self.server=None
        # The minute trigger resumes a persisted interrupted trial without the cloud.
        self.wait(lambda:read_json(self.root/b.STATE).get('phase')=='rolled_back',240)
        self.assertEqual(b.digest(self.root/b.SUPERVISOR),b.digest(self.binaries[0]));self.preserved()
    def test_b_full_business_trial_commits_without_interactive_install(self):
        self.wait(lambda:read_json(self.root/b.STATE).get('phase')=='trial',240)
        with closing(sqlite3.connect(self.data/'dashboard_data.sqlite3')) as db,db:db.execute("INSERT INTO bootstrap_preserved_evidence VALUES('during migration')")
        started=time.monotonic()
        self.wait(lambda:read_json(self.root/b.STATE).get('phase')=='committed',750)
        self.assertGreater(time.monotonic()-started,590)
        self.assertEqual(b.digest(self.root/b.SUPERVISOR),b.digest(self.bridge));self.preserved()
    def cleanup(self):
        for task in (b.WindowsBackend.TASK,wi.TASK_NAME):
            wi.run_checked(['schtasks.exe','/End','/TN',task],allow_failure=True)
            wi.run_checked(['schtasks.exe','/Delete','/TN',task,'/F'],allow_failure=True)
        time.sleep(5)
        if getattr(self,'server',None):self.server.shutdown();self.server.server_close();self.thread.join(5)
        if self.root.exists():shutil.rmtree(self.root)
        if self.data.exists():shutil.rmtree(self.data)
