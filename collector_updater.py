"""Stable Windows supervisor: stage, verify, restart, health-check, rollback."""
from __future__ import annotations

import json
import math
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from cloud_tls import cloud_ssl_context
from collector_startup import record_startup
from collector_settings import load_settings
from single_instance import SingleInstanceLock
from update_protocol import (MAX_EXE_BYTES, atomic_json as _atomic_json, read_json, secure_origin,
                             verify_executable, verify_manifest, verify_scope, version_tuple)

CHECK_SECONDS = 3600
HEALTH_TIMEOUT = 120
HEALTH_STABLE_SECONDS = 30
PROBATION_SECONDS = 600
EXE_NAME = 'HawkHive-DPC8001-Collector.exe'


def retry_file_operation(operation, *args):
    for attempt in range(3):
        try:
            return operation(*args)
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(.2)


def atomic_json(path, payload):
    return retry_file_operation(_atomic_json, path, payload)



class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Update redirects are not allowed')


def update_opener():
    return urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=cloud_ssl_context()))


def download_release(origin: str, release: dict, staged: Path, opener=None) -> None:
    origin = secure_origin(origin)
    opener = opener or update_opener()
    # Keep room for staging, rollback and PyInstaller extraction. No disk cleanup
    # ever deletes measurement data to make space for an update.
    if shutil.disk_usage(staged.parent).free < max(512*1024*1024, release['size']*4):
        raise OSError('Not enough disk space for a safe update')
    request = urllib.request.Request(origin + '/api/v1/collector-updates/' + release['sha256'] + '.exe', headers={'User-Agent':'HawkHive-Collector-Updater/1'})
    try:
        with opener.open(request, timeout=30) as response, staged.open('wb') as stream:
            if response.geturl() != request.full_url:
                raise ValueError('Unexpected update download URL')
            total = 0
            for chunk in iter(lambda: response.read(1024*1024), b''):
                total += len(chunk)
                if total > min(release['size'], MAX_EXE_BYTES):
                    raise ValueError('Update download exceeds signed size')
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        verify_executable(staged, release)
    except Exception:
        staged.unlink(missing_ok=True)
        raise


class UpdateTransaction:
    """Only EXE files change. Configuration and SQLite/WAL files are untouched."""
    def __init__(self, install_dir: Path):
        self.root = install_dir
        self.exe = install_dir / EXE_NAME
        self.staged = install_dir / 'collector-update.download'
        self.previous = install_dir / 'collector-previous.exe'
        self.journal = install_dir / 'collector-update-transaction.json'
        self.installed = install_dir / 'collector-installed.json'

    def switch(self, release: dict, previous_version: str) -> None:
        verify_executable(self.staged, release)
        shutil.copy2(self.exe, self.previous)
        with self.previous.open("r+b") as stream:
            os.fsync(stream.fileno())
        atomic_json(self.journal, {'target': release['version'], 'previous_version': previous_version,
                                  'target_sha256': release['sha256'],
                                  'previous_sha256': hashlib.sha256(self.previous.read_bytes()).hexdigest()})
        # An interrupted transaction (even before this rename) restores the known
        # previous executable on the next boot, before any new worker is started.
        retry_file_operation(os.replace, self.staged, self.exe)

    def commit(self, version: str) -> None:
        atomic_json(self.installed, {'version': version})
        self.journal.unlink(missing_ok=True)

    def recover(self) -> dict:
        if not self.journal.exists():
            return {}
        transaction = read_json(self.journal)
        if not transaction.get('target') or not transaction.get('previous_version'):
            raise RuntimeError('Update journal is unreadable; refusing to start an uncommitted binary')
        if transaction.get('previous_sha256') and self.exe.is_file() and hashlib.sha256(self.exe.read_bytes()).hexdigest() == transaction['previous_sha256']:
            atomic_json(self.installed, {'version': transaction['previous_version']})
            self.journal.unlink()
            return transaction  # Switch never happened, or recovery already completed.
        if not self.previous.is_file():
            raise RuntimeError('Interrupted update has no rollback executable')
        if transaction.get('previous_sha256') and hashlib.sha256(self.previous.read_bytes()).hexdigest() != transaction['previous_sha256']:
            raise RuntimeError('Rollback executable checksum mismatch')
        # Copy then rename preserves rollback if the machine loses power again.
        shutil.copy2(self.previous, self.staged)
        with self.staged.open("r+b") as stream:
            os.fsync(stream.fileno())
        retry_file_operation(os.replace, self.staged, self.exe)
        atomic_json(self.installed, {'version': transaction.get('previous_version')})
        self.journal.unlink()
        return transaction


def _contain_windows_children():
    """Children exit with the task, so Task Scheduler cannot leave orphan readers."""
    import ctypes
    from ctypes import wintypes
    class BasicLimit(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]
    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
    class ExtendedLimit(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', BasicLimit), ('IoInfo', IoCounters),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    handle = kernel.CreateJobObjectW(None, None)
    limits = ExtendedLimit()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    return handle  # Keep handle open for the entire supervisor lifetime.


def _watch_windows_bootstrap_parent():
    """Task Scheduler owns the onefile bootloader, outside our Python job.

    If Scheduler ends that outer process, terminate this supervisor too. Closing
    its job handle then also terminates every owned collection worker.
    """
    import ctypes
    import threading
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    parent = kernel.OpenProcess(0x100000, False, os.getppid())  # SYNCHRONIZE
    if not parent:
        raise ctypes.WinError(ctypes.get_last_error())
    def watch():
        result = kernel.WaitForSingleObject(parent, 0xFFFFFFFF)
        kernel.CloseHandle(parent)
        # Both parent death and an invalid wait must fail closed, never leave an
        # unowned collector. The OS closes our job handle even on abrupt exit.
        os._exit(1 if result == 0 else 2)
    threading.Thread(target=watch, name='bootstrap-lifetime', daemon=True).start()


class Supervisor:
    def __init__(self, install_dir: Path, data_dir: Path, port: int):
        self.transaction = UpdateTransaction(install_dir)
        self.data_dir = data_dir
        self.port = port
        self.status_path = data_dir / 'collector-update-status.json'
        self.stop_path = data_dir / 'collector-supervisor-stop.json'
        self.child = None
        self.nonce = ''
        self.current = read_json(self.transaction.installed).get('version')
        self.status = read_json(self.status_path)
        self.update_state_readable = not self.status_path.exists() or bool(self.status)
        if not self.update_state_readable:
            self.status = {'updates_disabled': True, 'error': 'Update status is unreadable; automatic installation disabled'}
        self.update_state_readable = self.update_state_readable and not self.status.get('updates_disabled', False)
        self.blocked_version = self.status.get('blocked_version')
        self.blocked_sha256 = self.status.get('blocked_sha256')
        self.local_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def report(self, state: str, **values):
        self.status.update(enabled=True, state=state, current_version=self.current, updated_at=time.time(), **values)
        atomic_json(self.status_path, self.status)

    def start_worker(self):
        self.nonce = secrets.token_hex(24)
        self.stop_path.unlink(missing_ok=True)
        env = {**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT':'1', 'DCP_SUPERVISOR_NONCE':self.nonce}
        env['DCP_UPDATE_TRIAL'] = '1' if self.transaction.journal.exists() else '0'
        env['DCP_UPDATE_JOURNAL'] = str(self.transaction.journal.resolve())
        from collector_activation import ACTIVATION_FILENAME
        from collector_enrollment import ENROLLMENT_FILENAME
        command = [str(self.transaction.exe),'--device','--no-browser','--managed-worker',
                   '--port',str(self.port),'--data-dir',str(self.data_dir),
                   '--activation',str(self.data_dir / ACTIVATION_FILENAME),
                   '--enrollment',str(self.data_dir / ENROLLMENT_FILENAME)]
        try:
            self.child = subprocess.Popen(command,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except OSError:
            record_startup(self.data_dir, 'worker_exited')
            raise

    def health(self) -> dict:
        try:
            with self.local_http.open(f'http://127.0.0.1:{self.port}/api/health',timeout=2) as response:
                result = json.loads(response.read(16384))
            data = result.get('data',{})
            if result.get('ok') and data.get('supervisor_nonce') == self.nonce and self.child.poll() is None:
                version_tuple(data['version'])
                return data
        except (OSError,ValueError,KeyError):
            pass
        return {}

    def stop_worker(self, *, force=False) -> bool:
        if self.child.poll() is not None:
            return True
        atomic_json(self.stop_path, {'nonce':self.nonce})
        try:
            self.child.wait(timeout=60)
            return True
        except subprocess.TimeoutExpired:
            if force:
                subprocess.run(['taskkill.exe','/PID',str(self.child.pid),'/T','/F'],capture_output=True,check=False,timeout=15)
                self.child.wait(timeout=30)
                return True
            self.stop_path.unlink(missing_ok=True)
            return False

    def healthy_trial(self, expected: str, *, business=False, scope=None) -> bool:
        stable_seconds = PROBATION_SECONDS if business else HEALTH_STABLE_SECONDS
        deadline = time.monotonic() + HEALTH_TIMEOUT + (stable_seconds if business else 0)
        healthy_since = None
        last_tick = None
        last_progress = time.monotonic()
        last_check = last_progress
        startup_deadline = last_progress + HEALTH_TIMEOUT
        invalid_since = None
        while time.monotonic() < deadline:
            if self.child.poll() is not None:
                return False
            data = self.health()
            now = time.monotonic()
            if business and now - last_check > 10:
                # Host suspend is not evidence against the new executable.
                healthy_since = None
                last_progress = now
                deadline = now + HEALTH_TIMEOUT + stable_seconds
                startup_deadline = now + HEALTH_TIMEOUT
            last_check = now
            valid = data.get('version') == expected
            if healthy_since is None and now > startup_deadline:
                return False
            if business:
                tick = data.get('scheduler', {}).get('tick')
                age = data.get('scheduler', {}).get('age_seconds')
                attempt_age = data.get('scheduler', {}).get('oldest_attempt_seconds')
                valid = (valid and data.get('health_contract') == 1
                         and (scope is None or data.get('update_scope') == scope)
                         and data.get('monitor_running') is True
                         and data.get('scheduler', {}).get('internal_failures') == 0
                         and data.get('storage_state') not in (None, 'critical', 'not_ready')
                         and type(tick) is int and tick > 0
                         and type(attempt_age) in (float, int) and math.isfinite(attempt_age) and 0 <= attempt_age < 180
                         and type(age) in (float, int) and math.isfinite(age) and 0 <= age < 30)
                if valid and (last_tick is None or tick > last_tick):
                    last_tick, last_progress = tick, now
                elif last_tick is not None and (tick != last_tick or now - last_progress >= 30):
                    valid = False
                if healthy_since is not None and not valid:
                    if invalid_since is None:
                        invalid_since = now
                    if now - invalid_since >= 30:
                        return False
                    time.sleep(2)
                    continue
                invalid_since = None
            if valid:
                if healthy_since is None:
                    healthy_since = now
                    if business:
                        deadline = now + stable_seconds + 40
                if now - healthy_since >= stable_seconds:
                    return True
            else:
                healthy_since = None
            time.sleep(2)
        return False

    def check_update(self):
        settings = load_settings(self.data_dir/'collector_settings.json')
        if not settings.configured or not self.current or not self.update_state_readable:
            return
        scope = self.health().get('update_scope', {})
        if scope.get('site_id') != settings.site_id or not all(scope.get(k) for k in ('customer_id','site_id','instance_id')):
            self.report('failed', error='Collector scope unavailable; update postponed')
            return
        origin = secure_origin(settings.cloud_url)
        self.report('checking', last_check_at=time.time(), error=None)
        req = urllib.request.Request(origin+'/api/v1/collector-updates/scoped/stable',
                                     headers={'User-Agent':'HawkHive-Collector-Updater/2',
                                              'Authorization':'Bearer ' + settings.token,
                                              'X-DCP-Collector-Instance':scope['instance_id']})
        with update_opener().open(req,timeout=15) as response:
            if response.geturl() != req.full_url:
                raise ValueError('Unexpected update manifest URL')
            raw = response.read(65537)
            if len(raw)>65536:
                raise ValueError('Update manifest too large')
            envelope = json.loads(raw)
        if envelope.get('available') is False:
            self.report('idle', available_version=None)
            return
        release = verify_manifest(envelope)
        verify_scope(release, scope)
        self.report('available', available_version=release['version'])
        if version_tuple(release['version']) <= version_tuple(self.current):
            self.report('up_to_date')
            return
        if release['version'] == self.blocked_version or release['sha256'] == self.blocked_sha256:
            self.report('rolled_back', error='This release previously failed its startup check; waiting for a newer release')
            return
        self.report('downloading')
        download_release(origin, release, self.transaction.staged)
        if not self.stop_worker():
            self.report('failed',error='Collector did not stop cleanly; installation postponed')
            return
        switched = False
        try:
            self.transaction.switch(release,self.current)
            switched = True
            self.report('installing')
            self.start_worker()
            if not self.healthy_trial(release['version'], business=True, scope=scope):
                raise RuntimeError('Updated collector did not pass its startup health check')
            self.transaction.commit(release['version'])
        except Exception:
            if switched:
                if not self.stop_worker(force=True):
                    raise RuntimeError('Cannot confirm new collector stopped; rollback remains pending')
            self.blocked_version = release['version']
            self.blocked_sha256 = release['sha256']
            # Persist the block before clearing the transaction: a power loss
            # must not make the failed release eligible again.
            self.report('rollback_pending',blocked_version=self.blocked_version,blocked_sha256=self.blocked_sha256,error='New version failed; restoring previous collector')
            self.transaction.recover()
            self.report('rolled_back',error='Previous collector restored')
            self.start_worker()
            raise
        # The transaction is already committed. A status write failure must not
        # stop the healthy worker or pretend an absent journal restored old code.
        self.current = release['version']
        self.blocked_version = None
        self.blocked_sha256 = None
        try:
            self.report('updated',last_success_at=time.time(),error=None,blocked_version=None,blocked_sha256=None)
        except OSError:
            self.update_state_readable = False

    def run(self):
        if self.transaction.journal.exists():
            pending = read_json(self.transaction.journal)
            self.blocked_version = pending.get('target') or self.blocked_version
            self.blocked_sha256 = pending.get('target_sha256') or self.blocked_sha256
            self.report('rollback_pending', blocked_version=self.blocked_version,blocked_sha256=self.blocked_sha256)
        recovered = self.transaction.recover()
        if recovered:
            self.current = recovered.get('previous_version')
            self.blocked_version = recovered.get('target')
            self.report('rolled_back', blocked_version=self.blocked_version,error='Interrupted update restored the previous collector')
        else:
            self.report('starting')
        self.start_worker()
        next_check = time.monotonic() + 60
        while True:
            if self.child.poll() is not None:
                record_startup(self.data_dir, 'worker_exited')
                self.report('restarting',error='Collector process exited; restarting')
                time.sleep(10)
                self.start_worker()
            health = self.health()
            if health:
                if self.status.get('state') in ('starting', 'restarting'):
                    record_startup(self.data_dir, 'ready')
                self.current = health['version']
                if self.status.get('state') in ('starting','restarting'):
                    self.report('idle',error=None)
            if health and time.monotonic() >= next_check:
                try:
                    self.check_update()
                except Exception as exc:
                    if self.transaction.journal.exists():
                        # Never let the generic restart loop launch a failed target
                        # when stopping/restoring it failed. Boot recovery owns it.
                        raise
                    if self.status.get('state') != 'rolled_back':
                        # Do not surface URLs/tokens or change collection behavior.
                        self.report('failed',error=f'Automatic update check failed ({type(exc).__name__}); will retry')
                next_check = time.monotonic() + CHECK_SECONDS
            time.sleep(2)


def run_supervisor(data_dir: Path, port: int) -> int:
    from windows_install import install_paths
    install_dir, expected_data = install_paths()
    if os.name != 'nt' or not getattr(sys,'frozen',False) or Path(sys.executable).resolve().parent != install_dir.resolve() or data_dir.resolve() != expected_data.resolve():
        raise RuntimeError('Automatic updates require the installed Windows collector')
    # Task Scheduler's process tree is contained before creating any workers.
    job_handle = _contain_windows_children()
    _watch_windows_bootstrap_parent()
    lock = SingleInstanceLock(install_dir/'supervisor.lock')
    lock.acquire()
    try:
        from windows_power import SystemSleepInhibitor
        # Retain wakefulness while the worker is stopped for an update/recovery.
        with SystemSleepInhibitor():
            Supervisor(install_dir,data_dir,port).run()
    finally:
        lock.release()
    return 0
