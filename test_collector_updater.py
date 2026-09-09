import base64
import hashlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
import cloud_api
import collector_updater as updater
import update_protocol as protocol
import collector_update_api as api


def signed_release(version='0.6.1', content=b'MZ-new-collector'):
    key=Ed25519PrivateKey.generate()
    public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex()
    payload=dict(version=version,platform='windows-x64',protocol=1,size=len(content),sha256=hashlib.sha256(content).hexdigest(),issued_at=int(time.time()),expires_at=int(time.time())+86400)
    envelope=dict(release=payload,key_id='qa',signature=base64.b64encode(key.sign(protocol.canonical(payload))).decode())
    return envelope, {'qa':public}


class Response(io.BytesIO):
    def __init__(self, body, url): super().__init__(body);self.url=url
    def geturl(self): return self.url


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.exe=self.root/updater.EXE_NAME;self.exe.write_bytes(b'MZ-known-good')
        self.content=b'MZ-new-collector'
        self.envelope,self.keys=signed_release(content=self.content)
        self.release=self.envelope['release']

    def tearDown(self): self.temp.cleanup()

    def test_signature_rejects_altered_unknown_key_expired_and_unsupported_manifests(self):
        self.assertEqual(protocol.verify_manifest(self.envelope,keys=self.keys)['version'],'0.6.1')
        for field,value in [('version','0.9.9'),('sha256','0'*64),('platform','linux'),('size',1024)]:
            altered={**self.envelope,'release':{**self.release,field:value}}
            with self.assertRaises(Exception):protocol.verify_manifest(altered,keys=self.keys)
        with self.assertRaises(Exception):protocol.verify_manifest(self.envelope,keys={})
        with self.assertRaises(ValueError):protocol.verify_manifest(self.envelope,keys=self.keys,now=time.time()+90000)
        for version in ('1.2','1.2.3.exe','../1.2.3','01.2.3'):
            with self.assertRaises(ValueError):protocol.version_tuple(version)

    def test_update_origin_rejects_plaintext_credentials_and_paths(self):
        for origin in ('http://example.test','https://user:password@example.test','https://example.test/evil','https://example.test/?redirect=x'):
            with self.assertRaises(ValueError):protocol.secure_origin(origin)
        self.assertEqual(protocol.secure_origin('https://example.test/'),'https://example.test')
        with self.assertRaises(ValueError):updater.NoRedirect().redirect_request(None,None,302,'',{},'http://example.test')

    def download(self, content):
        origin='https://example.test';path=self.root/'download'
        url=origin+'/api/v1/collector-updates/'+self.release['sha256']+'.exe'
        opener=Mock();opener.open.return_value=Response(content,url)
        with patch.object(updater.shutil,'disk_usage',return_value=SimpleNamespace(free=1024**3)):
            updater.download_release(origin,self.release,path,opener)
        return path

    def test_download_verifies_exact_bytes_and_removes_partial_or_corrupt_files(self):
        self.assertEqual(self.download(self.content).read_bytes(),self.content)
        for content in (b'MZ-wrong',self.content[:-1],self.content+b'extra'):
            with self.assertRaises(ValueError):self.download(content)
            self.assertFalse((self.root/'download').exists())
        self.assertEqual(self.exe.read_bytes(),b'MZ-known-good')

    def test_disk_shortage_never_downloads_or_stops_collection(self):
        opener=Mock()
        with patch.object(updater.shutil,'disk_usage',return_value=SimpleNamespace(free=100)):
            with self.assertRaises(OSError):updater.download_release('https://example.test',self.release,self.root/'download',opener)
        opener.open.assert_not_called()
        self.assertEqual(self.exe.read_bytes(),b'MZ-known-good')

    def test_interrupted_update_restores_previous_binary_without_touching_runtime_data(self):
        runtime=self.root/'collector.sqlite3';runtime.write_bytes(b'pending rows and settings')
        transaction=updater.UpdateTransaction(self.root);transaction.staged.write_bytes(self.content)
        transaction.switch(self.release,'0.6.0')
        self.assertEqual(self.exe.read_bytes(),self.content)
        result=updater.UpdateTransaction(self.root).recover()
        self.assertEqual(result['target'],'0.6.1')
        self.assertEqual(self.exe.read_bytes(),b'MZ-known-good')
        self.assertEqual(runtime.read_bytes(),b'pending rows and settings')
        self.assertEqual(transaction.recover(),{})

    def test_committed_update_survives_supervisor_restart(self):
        transaction=updater.UpdateTransaction(self.root);transaction.staged.write_bytes(self.content)
        transaction.switch(self.release,'0.6.0');transaction.commit('0.6.1')
        self.assertEqual(transaction.recover(),{})
        self.assertEqual(self.exe.read_bytes(),self.content)
        self.assertEqual(protocol.read_json(transaction.installed)['version'],'0.6.1')

    def supervisor(self):
        supervisor=updater.Supervisor(self.root,self.root,8787)
        supervisor.current='0.6.0'
        supervisor.start_worker=Mock()
        supervisor.stop_worker=Mock(return_value=True)
        supervisor.child=Mock()
        return supervisor

    def run_check(self, supervisor, envelope=None):
        envelope=envelope or self.envelope
        opener=Mock();opener.open.return_value=Response(json.dumps(envelope).encode(),'https://example.test/api/v1/collector-updates/stable')
        def download(origin,release,path):path.write_bytes(self.content)
        with patch.object(updater,'load_settings',return_value=SimpleNamespace(configured=True,cloud_url='https://example.test')),patch.object(updater,'update_opener',return_value=opener),patch.object(updater,'download_release',side_effect=download),patch.dict(protocol.TRUSTED_KEYS,self.keys):
            supervisor.check_update()

    def test_failed_trial_rolls_back_and_does_not_retry_same_bad_release(self):
        supervisor=self.supervisor();supervisor.healthy_trial=Mock(return_value=False)
        with self.assertRaises(RuntimeError):self.run_check(supervisor)
        self.assertEqual(self.exe.read_bytes(),b'MZ-known-good')
        self.assertEqual(supervisor.status['state'],'rolled_back')
        self.assertEqual(supervisor.blocked_version,'0.6.1')
        self.assertEqual(supervisor.start_worker.call_count,2)
        supervisor.stop_worker.reset_mock()
        self.run_check(supervisor)
        supervisor.stop_worker.assert_not_called()

    def test_new_release_commits_only_after_health_and_downgrades_do_not_stop_worker(self):
        supervisor=self.supervisor();supervisor.healthy_trial=Mock(return_value=True)
        self.run_check(supervisor)
        self.assertEqual(supervisor.current,'0.6.1')
        self.assertEqual(supervisor.status['state'],'updated')
        supervisor.stop_worker.reset_mock()
        self.run_check(supervisor)
        supervisor.stop_worker.assert_not_called()

    def test_failed_clean_shutdown_keeps_old_binary(self):
        supervisor=self.supervisor();supervisor.stop_worker.return_value=False
        self.run_check(supervisor)
        self.assertEqual(self.exe.read_bytes(),b'MZ-known-good')
        supervisor.start_worker.assert_not_called()
        self.assertEqual(supervisor.status['state'],'failed')

    def test_health_rejects_other_instance_even_if_port_responds(self):
        supervisor=self.supervisor();supervisor.nonce='mine';supervisor.child.poll.return_value=None
        supervisor.local_http=Mock()
        for nonce,expected in [('other',False),('mine',True)]:
            supervisor.local_http.open.return_value=Response(json.dumps({'ok':True,'data':{'version':'0.6.1','supervisor_nonce':nonce}}).encode(),'')
            self.assertEqual(bool(supervisor.health()),expected)

    def test_distribution_serves_only_verified_manifest_and_digest_named_artifact(self):
        from fastapi import FastAPI
        app=FastAPI();api.register_update_routes(app)
        with patch.object(api,'release_directory',return_value=self.root),patch.dict(protocol.TRUSTED_KEYS,self.keys),TestClient(app) as http:
            self.assertEqual(http.get('/api/v1/collector-updates/stable').json(),{'available':False})
            artifact=self.root/(self.release['sha256']+'.exe')
            artifact.write_bytes(self.content)
            protocol.atomic_json(self.root/'stable.json',self.envelope)
            response=http.get('/api/v1/collector-updates/stable')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['cache-control'],'no-store')
            self.assertEqual(response.json()['release']['version'],'0.6.1')
            self.assertEqual(http.get('/api/v1/collector-updates/'+artifact.name).content,self.content)
            self.assertEqual(http.get('/api/v1/collector-updates/config.json').status_code,404)
            artifact.write_bytes(b'MZ-tampered')
            self.assertEqual(http.get('/api/v1/collector-updates/stable').status_code,503)

    def test_heartbeat_update_status_is_bounded_and_rejects_unknown_commands(self):
        from pydantic import ValidationError
        status=cloud_api.CollectorUpdateStatus(enabled=True,state='rolled_back',current_version='0.6.0',blocked_version='0.6.1')
        self.assertTrue(status.enabled)
        with self.assertRaises(ValidationError):cloud_api.CollectorUpdateStatus(state='execute')
        with self.assertRaises(ValidationError):cloud_api.CollectorUpdateStatus(error='x'*501)
