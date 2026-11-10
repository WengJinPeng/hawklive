"""Opt-in signed supervisor migration. No database or settings writes.

A bridge worker prepares this transaction only after its legacy update commits.
A separate SYSTEM scheduled task owns switching and crash recovery. Merely
shipping this module does not activate it or relax legacy distribution scope.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time

from single_instance import SingleInstanceLock
from update_protocol import TRUSTED_KEYS, canonical, atomic_json, read_json

SUPERVISOR = 'HawkHive-Collector-Supervisor.exe'
WORKER = 'HawkHive-DPC8001-Collector.exe'
HELPER = 'HawkHive-Bootstrap.exe'
STATE = 'supervisor-bootstrap.json'
TERMINAL = {'committed', 'rolled_back'}


def digest(path):
    with Path(path).open('rb') as stream:
        value=hashlib.sha256()
        for chunk in iter(lambda:stream.read(1024*1024),b''):value.update(chunk)
        return value.hexdigest()


def verify_plan(envelope, *, scope=None, keys=None, fresh=True):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    p = envelope['plan']
    Ed25519PublicKey.from_public_bytes(bytes.fromhex((TRUSTED_KEYS if keys is None else keys)[envelope['key_id']])).verify(
        base64.b64decode(envelope['signature'], validate=True), canonical(p))
    if p.get('purpose') != 'supervisor-bootstrap-v1':
        raise ValueError('Wrong bootstrap purpose')
    if not re.fullmatch(r'[a-f0-9]{32}', p.get('id', '')):
        raise ValueError('Invalid migration id')
    if set(p.get('scope', {})) != {'customer_id', 'site_id', 'instance_id'} or not all(isinstance(x,str) and x for x in p['scope'].values()):
        raise ValueError('Invalid scope')
    if scope is not None and p['scope'] != scope:
        raise ValueError('Wrong migration scope')
    for key in ('previous_sha256', 'target_sha256', 'worker_sha256'):
        if not re.fullmatch('[a-f0-9]{64}', p.get(key, '')):
            raise ValueError('Invalid digest')
    if type(p.get('issued_at')) is not int or type(p.get('expires_at')) is not int or not 0 < p['expires_at']-p['issued_at'] <= 86400:
        raise ValueError('Invalid validity period')
    if fresh and not p['issued_at'] <= time.time() < p['expires_at']:
        raise ValueError('Expired migration')
    if p.get('trial_seconds') != 600:
        raise ValueError('Full business trial required')
    return p


def durable_copy(source, destination, expected):
    if digest(source) != expected:
        raise ValueError('Source digest mismatch')
    temporary = Path(str(destination)+'.new')
    shutil.copyfile(source, temporary)
    with temporary.open('r+b') as stream:
        os.fsync(stream.fileno())
    if digest(temporary) != expected:
        raise ValueError('Staged digest mismatch')
    os.replace(temporary, destination)


class Migration:
    def __init__(self, root, data, backend, *, keys=None):
        self.root, self.data, self.backend = Path(root), Path(data), backend
        self.path = self.root / STATE
        self.keys = keys

    def save(self, record, phase):
        record['phase'] = phase
        atomic_json(self.path, record)

    def prepare(self, envelope):
        with SingleInstanceLock(self.root/'bootstrap.lock'):
            if self.path.exists():
                raise ValueError('Migration already recorded; never auto retry')
            p = verify_plan(envelope, scope=self.backend.scope(), keys=self.keys)
            self.backend.preflight()
            if (self.root/'collector-update-transaction.json').exists():
                raise ValueError('Legacy worker transaction has not committed')
            installed = read_json(self.root/'collector-installed.json')
            status = read_json(self.data/'collector-update-status.json')
            if not installed.get('version') or status.get('state') != 'updated' or status.get('current_version') != installed['version']:
                raise ValueError('Legacy commit receipt unavailable')
            for name, key in ((SUPERVISOR,'previous_sha256'), (WORKER,'worker_sha256')):
                if digest(self.root/name) != p[key]:
                    raise ValueError('Unexpected installed binary')
            # Candidate is the committed bridge itself; no arbitrary file paths.
            if p['target_sha256'] != p['worker_sha256']:
                raise ValueError('Candidate must be the committed bridge')
            backup = self.root/'bootstrap-previous.exe'
            durable_copy(self.root/SUPERVISOR, backup, p['previous_sha256'])
            durable_copy(self.root/WORKER, self.root/HELPER, p['target_sha256'])
            record = {'envelope':envelope, 'phase':'prepared', 'version':installed['version']}
            self.save(record, 'prepared')
            # Register persistent boot/repetition recovery before touching main task.
            self.backend.register_recovery(self.root/HELPER)
            self.backend.verify_recovery(self.root/HELPER)
            self.save(record, 'armed')
            self.backend.launch_recovery()

    def run(self):
        with SingleInstanceLock(self.root/'bootstrap.lock'):
            if not self.path.exists():
                return 'absent'
            r = read_json(self.path)
            p = verify_plan(r['envelope'], keys=self.keys, fresh=False)
            phase = r['phase']
            if phase in TERMINAL:
                return phase
            if phase == 'prepared':
                return 'prepared'  # registration interrupted; main collector untouched
            if phase not in {'armed','stopping','stopped','switching','trial','rolling_back'}:
                raise ValueError('Unknown migration state')
            # Never start a fresh migration after an expired authorization.
            if phase == 'armed':
                verify_plan(r['envelope'], scope=self.backend.scope(), keys=self.keys)
                if (self.root/'collector-update-transaction.json').exists():
                    raise ValueError('Legacy transaction reappeared')
                if digest(self.root/WORKER) != p['worker_sha256']:
                    raise ValueError('Bridge changed since preparation')
                self.backend.verify_recovery(self.root/HELPER)
                self.save(r, 'stopping')
                try:
                    self.backend.stop_and_wait()
                    self.save(r, 'stopped')
                    self.save(r, 'switching')
                    durable_copy(self.root/HELPER, self.root/SUPERVISOR, p['target_sha256'])
                    self.save(r, 'trial')
                    self.backend.start()
                    if not self.backend.trial(p['scope'], r['version'], p['trial_seconds']):
                        raise RuntimeError('Business trial failed')
                    self.save(r, 'committed')
                    return 'committed'
                except Exception:
                    return self.rollback(r,p)
            # A restarted helper cannot prove uninterrupted trial. Restore offline.
            return self.rollback(r,p)

    def rollback(self, r, p):
        # Verify recovery material BEFORE stopping anything. Never guess a backup.
        if digest(self.root/'bootstrap-previous.exe') != p['previous_sha256']:
            raise ValueError('Recovery backup damaged')
        self.save(r, 'rolling_back')
        self.backend.stop_and_wait()
        durable_copy(self.root/'bootstrap-previous.exe', self.root/SUPERVISOR, p['previous_sha256'])
        self.backend.start()
        if not self.backend.recovered(r['version']):
            raise RuntimeError('Old supervisor has not recovered; persistent task will retry')
        self.save(r, 'rolled_back')
        return 'rolled_back'


class WindowsBackend:
    TASK = 'HawkHiveSupervisorBootstrap'

    def __init__(self, root, data):
        self.root, self.data = Path(root), Path(data)

    def command(self, args):
        from windows_install import run_checked
        run_checked(args)

    def xml(self, name):
        import subprocess
        return subprocess.check_output(['schtasks.exe','/Query','/TN',name,'/XML'],
                                       encoding='oem',errors='strict',timeout=15)

    def check_task(self, name, executable, arguments):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(self.xml(name))
        ns = {'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        if len(root.findall('t:Actions/t:Exec',ns)) != 1:
            raise ValueError('Unexpected task actions')
        command = root.findtext('t:Actions/t:Exec/t:Command',namespaces=ns)
        args = root.findtext('t:Actions/t:Exec/t:Arguments',default='',namespaces=ns)
        if (Path(command).resolve() != executable.resolve() or args != arguments
                or root.findtext('t:Principals/t:Principal/t:UserId',namespaces=ns) != 'S-1-5-18'):
            raise ValueError('Unexpected task identity or command')
        return root,ns

    def preflight(self):
        import subprocess
        from windows_install import install_paths, TASK_NAME, scheduled_task_arguments
        if os.name != 'nt' or tuple(p.resolve() for p in install_paths()) != (self.root.resolve(),self.data.resolve()):
            raise ValueError('Only canonical installed runtime supported')
        user = subprocess.check_output(['whoami.exe','/user','/fo','csv','/nh'],encoding='oem',timeout=10)
        if '"S-1-5-18"' not in user:
            raise PermissionError('SYSTEM required; no elevation prompt allowed')
        self.check_task(TASK_NAME, self.root/SUPERVISOR, scheduled_task_arguments(self.data))
        if shutil.disk_usage(self.root).free < (self.root/WORKER).stat().st_size*4+100*1024*1024:
            raise OSError('Insufficient recovery space')
        if any(p.is_symlink() for p in (self.root,self.data,self.root/SUPERVISOR,self.root/WORKER)):
            raise ValueError('Unexpected symbolic link')

    def health(self):
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:8787/api/health',timeout=2) as response:
            value = json.loads(response.read(16385))
        if not value.get('ok'):
            raise ValueError('Health not ready')
        return value['data']

    def scope(self):
        from collector_settings import load_settings
        scope = self.health().get('update_scope',{})
        if scope.get('site_id') != load_settings(self.data/'collector_settings.json').site_id:
            raise ValueError('Local site mismatch')
        return scope

    def register_recovery(self, helper):
        from datetime import datetime, timedelta
        from xml.sax.saxutils import escape
        from windows_install import scheduled_task_xml
        xml = scheduled_task_xml(helper,self.data)
        # Task Scheduler, not the worker's process tree, launches the helper.
        start=(datetime.now()+timedelta(minutes=1)).replace(microsecond=0).isoformat()
        xml=xml.replace('</Triggers>',f'<TimeTrigger><Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>')
        import re
        xml=re.sub(r'<Arguments>.*?</Arguments>','<Arguments>--bootstrap-run</Arguments>',xml,flags=re.S)
        path=self.root/'bootstrap-task.xml'
        path.write_text(xml,encoding='utf-16')
        # Do not overwrite a task created by a different installation.
        import subprocess
        exists=subprocess.run(['schtasks.exe','/Query','/TN',self.TASK],capture_output=True,timeout=15)
        if exists.returncode == 0:
            raise ValueError('Recovery task already exists')
        self.command(['schtasks.exe','/Create','/TN',self.TASK,'/XML',str(path)])

    def verify_recovery(self, helper):
        root,ns=self.check_task(self.TASK,helper,'--bootstrap-run')
        if (root.find('t:Triggers/t:BootTrigger',ns) is None
                or root.findtext('t:Triggers/t:TimeTrigger/t:Repetition/t:Interval',namespaces=ns) != 'PT1M'
                or root.findtext('t:Settings/t:MultipleInstancesPolicy',namespaces=ns) != 'IgnoreNew'):
            raise ValueError('Persistent recovery triggers missing')

    def launch_recovery(self):
        self.command(['schtasks.exe','/Run','/TN',self.TASK])

    def stop_and_wait(self):
        from windows_install import TASK_NAME
        from single_instance import MachineInstanceLock, AlreadyRunningError
        self.command(['schtasks.exe','/Change','/TN',TASK_NAME,'/Disable'])
        from windows_install import run_checked
        run_checked(['schtasks.exe','/End','/TN',TASK_NAME],allow_failure=True)
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            machine=MachineInstanceLock()
            try:
                with SingleInstanceLock(self.root/'supervisor.lock'):
                    machine.acquire()
                    return
            except (AlreadyRunningError,OSError):
                time.sleep(.5)
            finally:
                machine.release()
        raise TimeoutError('Owned collector has not released its locks')

    def start(self):
        from windows_install import TASK_NAME
        self.command(['schtasks.exe','/Change','/TN',TASK_NAME,'/Enable'])
        self.command(['schtasks.exe','/Run','/TN',TASK_NAME])

    def trial(self, scope, version, seconds):
        from collector_updater import Supervisor
        backend=self
        class Child:
            def poll(self):return None
        observer=Supervisor.__new__(Supervisor)
        observer.child=Child()
        nonce=[None]
        def health():
            try:
                data=backend.health()
                if data.get('supervisor_protocol') != 2 or not data.get('supervisor_nonce'):
                    return {}
                nonce[0]=nonce[0] or data['supervisor_nonce']
                if data['supervisor_nonce'] != nonce[0]:return {}
                return data
            except (OSError,ValueError):return {}
        observer.health=health
        # Supervisor business trial defaults to the signed 600-second contract.
        return observer.healthy_trial(version,business=True,scope=scope)

    def recovered(self, version):
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            try:
                if self.health().get('version') == version:return True
            except (OSError,ValueError):pass
            time.sleep(2)
        return False


def run_installed():
    from windows_install import install_paths
    import sys
    root,data=install_paths()
    if os.name != 'nt' or not getattr(sys,'frozen',False) or Path(sys.executable).resolve() != (root/HELPER).resolve():
        raise ValueError('Only installed recovery helper may run')
    from collector_updater import _contain_windows_children, _watch_windows_bootstrap_parent
    job = _contain_windows_children()
    _watch_windows_bootstrap_parent()
    backend=WindowsBackend(root,data)
    backend.preflight()
    return Migration(root,data,backend).run()


def poll_authorization(service):
    """Authenticated fixed-purpose request; no plan means no local mutation."""
    import sys
    import urllib.request
    from cloud_tls import cloud_urlopen
    from collector_settings import load_settings
    from update_protocol import secure_origin
    from windows_install import install_paths
    root,data=install_paths()
    if (os.name != 'nt' or not getattr(sys,'frozen',False)
            or Path(sys.executable).resolve() != (root/WORKER).resolve()
            or Path(service.db_path).parent.resolve() != data.resolve()
            or not service.customer_id or (root/STATE).exists()):
        return 0
    # Only the bridge's already authenticated customer/site/instance is eligible.
    scope=dict(customer_id=service.customer_id,site_id=service.site_id,instance_id=service.instance_id)
    settings=load_settings(data/'collector_settings.json')
    req=urllib.request.Request(secure_origin(settings.cloud_url)+'/api/v1/collector-updates/bootstrap/plan',
                              headers=service._headers())
    try:
        with cloud_urlopen(req,timeout=10) as response:
            if response.geturl()!=req.full_url:raise ValueError('Bootstrap redirect refused')
            raw=response.read(65537)
    except urllib.error.HTTPError as error:
        if error.code==404:return 0
        raise
    if len(raw)>65536:raise ValueError('Oversized migration plan')
    envelope=json.loads(raw)
    if envelope.get('available') is False:return 0
    verify_plan(envelope,scope=scope)
    Migration(root,data,WindowsBackend(root,data)).prepare(envelope)
    return 1
