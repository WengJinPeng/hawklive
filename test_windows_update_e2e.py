"""Opt-in actual Windows EXE lifecycle checks on a disposable Windows machine."""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from collector_updater import Supervisor, EXE_NAME
from cloud_sync import COLLECTOR_VERSION


@unittest.skipUnless(os.name=='nt' and os.environ.get('DCP_UPDATER_ISOLATED_WINDOWS_QA')=='1',
                     'Disposable Windows environment and built EXE required')
class FrozenWindowsUpdateTests(unittest.TestCase):
    def setUp(self):
        self.source=Path(os.environ['DCP_UPDATE_TEST_EXE']).resolve()
        self.temp=tempfile.TemporaryDirectory(prefix='hawkhive-update-qa-')
        self.root=Path(self.temp.name)
        (self.root/'app').mkdir();(self.root/'data').mkdir()
        shutil.copy2(self.source,self.root/'app'/EXE_NAME)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        self.supervisor=Supervisor(self.root/'app',self.root/'data',port)
        self.supervisor.start_worker()
        self.addCleanup(self.cleanup_worker)
        with patch('collector_updater.HEALTH_STABLE_SECONDS',2):
            self.assertTrue(self.supervisor.healthy_trial(COLLECTOR_VERSION),'Real frozen collector must start with matching nonce and healthy storage')
        self.supervisor.current=COLLECTOR_VERSION
        self.assertEqual(self.supervisor.health()['sleep_prevention'],
                         {'state': 'active', 'system_required': True})

    def cleanup_worker(self):
        if self.supervisor.child:
            self.supervisor.stop_worker(force=True)
        self.temp.cleanup()

    def test_real_file_lock_release_replacement_and_failed_trial_rollback(self):
        # Reinstall the actual build to exercise Windows/PyInstaller handles.
        # Version ordering/signatures are covered independently by protocol tests.
        tx=self.supervisor.transaction
        release={'version':COLLECTOR_VERSION,'size':self.source.stat().st_size,'sha256':hashlib.sha256(self.source.read_bytes()).hexdigest()}
        tx.staged.write_bytes(self.source.read_bytes())
        self.assertTrue(self.supervisor.stop_worker())
        tx.switch(release,COLLECTOR_VERSION)
        self.supervisor.start_worker()
        with patch('collector_updater.HEALTH_STABLE_SECONDS',2):
            self.assertTrue(self.supervisor.healthy_trial(COLLECTOR_VERSION))
        tx.commit(COLLECTOR_VERSION)
        # A syntactically valid but non-runnable PE must restore the actual build.
        self.assertTrue(self.supervisor.stop_worker())
        broken=b'MZ-invalid-update'
        tx.staged.write_bytes(broken)
        tx.switch({'version':'999.0.0','size':len(broken),'sha256':hashlib.sha256(broken).hexdigest()},COLLECTOR_VERSION)
        with self.assertRaises(OSError): self.supervisor.start_worker()
        tx.recover()
        self.supervisor.start_worker()
        with patch('collector_updater.HEALTH_STABLE_SECONDS',2):
            self.assertTrue(self.supervisor.healthy_trial(COLLECTOR_VERSION))
        self.assertTrue((self.root/'data'/'collector.sqlite3').exists() or any((self.root/'data').glob('*.sqlite3')))

    def test_job_object_terminates_only_owned_child_on_supervisor_exit(self):
        pid_path=self.root/'child.pid'
        script='''import subprocess,sys,time
from collector_updater import _contain_windows_children
job=_contain_windows_children()
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])
open(sys.argv[1],'w').write(str(child.pid))
'''
        result=subprocess.run([os.sys.executable,'-c',script,str(pid_path)],timeout=15)
        self.assertEqual(result.returncode,0)
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_bool,ctypes.c_ulong]
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x100000,False,int(pid_path.read_text()))
        if handle:
            try:self.assertEqual(kernel.WaitForSingleObject(handle,5000),0)
            finally:kernel.CloseHandle(handle)

    def test_system_task_starts_supervisor_and_stops_owned_worker(self):
        import windows_install
        import urllib.request
        from urllib.error import URLError
        install_dir,data_dir=windows_install.install_paths()
        self.assertFalse(install_dir.exists(),'Requires a disposable Windows host without HawkHive installed')
        self.assertFalse(data_dir.exists(),'Must not touch any existing collector data')
        self.assertTrue(windows_install.is_administrator(),'Windows installer test requires an elevated runner')
        self.assertTrue(self.supervisor.stop_worker())
        installed=False
        try:
            windows_install.install_elevated(self.source,None)
            installed=True
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline=time.monotonic()+120
            healthy=False
            while time.monotonic()<deadline:
                try:
                    with opener.open('http://127.0.0.1:8787/api/health',timeout=2) as r:result=json.load(r)
                    if result.get('ok') and result['data'].get('version')==COLLECTOR_VERSION and result['data'].get('supervisor_nonce'):
                        healthy=True;break
                except (OSError,ValueError):pass
                time.sleep(2)
            self.assertTrue(healthy,'Installed SYSTEM task must run a healthy supervised collector')
            self.assertTrue(result['data']['sleep_prevention']['system_required'])
            requests = subprocess.check_output(['powercfg.exe', '/requests']).replace(b'\x00', b'').lower()
            self.assertIn(b'hawkhive-dpc8001-collector.exe', requests)
            self.assertIn(b'hawkhive-collector-supervisor.exe', requests)
            xml=subprocess.check_output(['schtasks.exe','/Query','/TN',windows_install.TASK_NAME,'/XML'],text=True)
            self.assertIn('HawkHive-Collector-Supervisor.exe',xml)
            self.assertIn('--supervise',xml)
            self.assertIn('S-1-5-18',xml)
            windows_install.run_checked(['schtasks.exe','/End','/TN',windows_install.TASK_NAME])
            time.sleep(3)
            with self.assertRaises(OSError):opener.open('http://127.0.0.1:8787/api/health',timeout=2)
            requests = subprocess.check_output(['powercfg.exe', '/requests']).replace(b'\x00', b'').lower()
            self.assertNotIn(b'hawkhive-dpc8001-collector.exe', requests)
            self.assertNotIn(b'hawkhive-collector-supervisor.exe', requests)
        finally:
            if installed or install_dir.exists():
                windows_install.run_checked(['schtasks.exe','/End','/TN',windows_install.TASK_NAME],allow_failure=True)
                windows_install.run_checked(['schtasks.exe','/Delete','/TN',windows_install.TASK_NAME,'/F'],allow_failure=True)
                # Paths were verified absent before this isolated test created them.
                shutil.rmtree(install_dir,ignore_errors=True)
                shutil.rmtree(data_dir,ignore_errors=True)
