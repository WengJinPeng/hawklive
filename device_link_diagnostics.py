"""Passive, bounded incident evidence. No device probes or configuration changes."""
from __future__ import annotations

import ipaddress
import json
import os
import queue
import subprocess
import threading
import time
from collections import OrderedDict

from collector_diagnostics import diagnostic_scope

# Temporary field investigation: an empty override disables collection entirely.
DEFAULT_HOSTS = '10.12.0.115,10.12.0.117,10.12.0.132'


def network_snapshot(host: str) -> dict:
    address = ipaddress.ip_address(host)
    if address.version != 4 or not address.is_private:
        return {'state': 'unsupported_address'}
    if os.name != 'nt':
        return {'state': 'unsupported_platform'}
    # Literal address is validated above. Do not enumerate command lines, SSIDs,
    # environment variables, adapter credentials, or arbitrary process details.
    script = """
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$r=@{}
try {$r.route=@(Find-NetRoute -RemoteIPAddress 'HOST' | Select-Object -First 2 -Property IPAddress,InterfaceIndex,DestinationPrefix,NextHop,RouteMetric)} catch {$r.route_error=$_.Exception.GetType().Name}
try {$r.neighbor=@(Get-NetNeighbor -IPAddress 'HOST' -ErrorAction Stop | Select-Object -First 2 -Property InterfaceIndex,LinkLayerAddress,@{Name='State';Expression={$_.State.ToString()}})} catch {$r.neighbor_error=$_.Exception.GetType().Name}
try {$r.tcp=@(Get-NetTCPConnection -RemoteAddress 'HOST' -ErrorAction Stop | Select-Object -First 8 -Property LocalAddress,LocalPort,RemotePort,@{Name='State';Expression={$_.State.ToString()}},OwningProcess)} catch {$r.tcp_error=$_.Exception.GetType().Name}
$r | ConvertTo-Json -Depth 4 -Compress
""".replace('HOST', str(address))
    import base64
    executable = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                              'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    try:
        result = subprocess.run(
            [executable, '-NoProfile', '-NonInteractive', '-EncodedCommand',
             base64.b64encode(script.encode('utf-16le')).decode('ascii')],
            capture_output=True, timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if result.returncode:
            return {'state': 'query_failed', 'returncode': result.returncode}
        if len(result.stdout) > 12000:
            return {'state': 'output_limit'}
        data = json.loads(result.stdout.decode('utf-8-sig'))
        return {'state': 'observed', **data} if isinstance(data, dict) else {'state': 'invalid_output'}
    except Exception as exc:
        return {'state': 'unavailable', 'error_type': type(exc).__name__}


class DeviceLinkDiagnostics:
    def __init__(self, emit, hosts=None, snapshot=network_snapshot):
        self.emit = emit
        self.hosts = frozenset(str(h).strip() for h in (hosts if hosts is not None else
                               os.environ.get('DCP_DIAGNOSTIC_HOSTS', DEFAULT_HOSTS).split(',')) if str(h).strip())
        self.snapshot = snapshot
        self.jobs = queue.Queue(maxsize=8)
        self.lock = threading.Lock()
        self.last = OrderedDict()
        self.thread = None
        self.closed = threading.Event()

    def submit(self, customer_id, site_id, device, evidence):
        """Never waits for OS queries, queue capacity, or log/database writes."""
        if self.closed.is_set() or device.get('host') not in self.hosts:
            return
        if not self.lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            # Keep connect-stage evidence even immediately after a read failure.
            frames = evidence.get('frames') or []
            header = frames[-1].get('header', '') if frames else ''
            category = ('connect' if evidence.get('stage') == 'connect' else
                        'at_text' if header.startswith('41542b') else
                        'protocol_error' if evidence.get('error_type') == 'ValueError' else 'read')
            key = (customer_id, site_id, str(device['id']), str(device['host']),
                   category)
            if now - self.last.get(key, float('-inf')) < 600:
                return
            job = (now, customer_id, site_id, str(device['id']), str(device['host']), dict(evidence))
            try:
                self.jobs.put_nowait(job)
            except queue.Full:
                return
            self.last[key] = now
            self.last.move_to_end(key)
            while len(self.last) > 128:
                self.last.popitem(last=False)
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name='dcp-link-diagnosis', daemon=True)
                self.thread.start()
        finally:
            self.lock.release()

    def close(self):
        self.closed.set()  # No join in the collection/shutdown path.

    def _run(self):
        while not self.closed.is_set():
            try:
                job = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                queued, customer, site, device, host, evidence = job
                if time.monotonic() - queued > 30:
                    continue
                try:
                    network = self.snapshot(host)
                except Exception as exc:
                    network = {'state': 'unavailable', 'error_type': type(exc).__name__}
                if self.closed.is_set():
                    continue
                # Separate bounded JSON events survive the existing 2000-char
                # sanitizer without truncating JSON; share incident correlation.
                incident = f'{device}:{time.time_ns()}'
                with diagnostic_scope(customer, site):
                    parts = [('protocol', evidence)] + [(k, v) for k, v in network.items()]
                    for part, data in parts:
                        payload = dict(v=1, incident=incident, device=device, host=host,
                                       captured_at=time.time(), part=part, data=data)
                        encoded = json.dumps(payload, separators=(',', ':'), ensure_ascii=True)
                        if len(encoded) > 1900:
                            payload['data'] = {'state': 'size_limit'}
                            encoded = json.dumps(payload, separators=(',', ':'))
                        self.emit('INFO', 'device_link_evidence', encoded)
            except Exception:
                pass  # Diagnosis failure must never stop collection or retry.
            finally:
                self.jobs.task_done()
