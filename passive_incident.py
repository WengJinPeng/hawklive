"""Opt-in, site-scoped cache diagnostics. Never opens a device socket."""
from __future__ import annotations
import ctypes as C
from collections import deque
import ipaddress
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from update_protocol import atomic_json, read_json

MAX_OUTPUT = 65536
BUDGET_SECONDS = 3


def cached_network(hosts):
    from device_identity import RouteRow, NeighborRow, sockaddr
    if len(hosts) > 8:
        raise ValueError('At most eight registered endpoints')
    addresses = [ipaddress.IPv4Address(host) for host in hosts]
    if any(not a.is_private or a.is_loopback or a.is_multicast for a in addresses):
        raise ValueError('Private endpoints required')
    if os.name != 'nt':
        return {'state':'unsupported_platform','endpoints':[]}
    api = C.WinDLL('iphlpapi.dll')
    api.GetBestRoute.argtypes = [C.c_uint32,C.c_uint32,C.POINTER(RouteRow)]
    api.GetBestRoute.restype = C.c_uint32
    api.GetIpNetEntry2.argtypes = [C.POINTER(NeighborRow)]
    api.GetIpNetEntry2.restype = C.c_uint32
    class IfRow(C.Structure):
        _fields_ = [('name',C.c_wchar * 256)] + [(n,C.c_uint32) for n in
            ('index','type','mtu','speed','physical_length')] + [('physical',C.c_ubyte * 8)] + [(n,C.c_uint32) for n in
            ('admin_status','oper_status','last_change','in_octets','in_unicast','in_nonunicast','in_discards','in_errors','in_unknown','out_octets','out_unicast','out_nonunicast','out_discards','out_errors','out_queue','description_length')] + [('description',C.c_ubyte * 256)]
    api.GetIfEntry.argtypes = [C.POINTER(IfRow)]
    api.GetIfEntry.restype = C.c_uint32
    endpoints=[];interfaces={}
    for address in addresses:
        route=RouteRow()
        err=api.GetBestRoute(int.from_bytes(address.packed,'little'),0,C.byref(route))
        item={'host':str(address),'route_status':int(err)}
        if not err:
            item.update(interface=int(route.index),next_hop=str(ipaddress.IPv4Address(int(route.next_hop).to_bytes(4,'little'))),route_metric=int(route.metric1))
            row=NeighborRow();row.address=sockaddr(str(address));row.index=route.index
            status=api.GetIpNetEntry2(C.byref(row))  # Cache only; NEVER ResolveIpNetEntry2.
            item.update(neighbor_status=int(status))
            if not status:
                item.update(neighbor_state=int(row.state),mac=':'.join(f'{b:02X}' for b in row.physical[:min(row.length,32)]))
            if route.index not in interfaces:
                interface=IfRow();interface.index=route.index
                status=api.GetIfEntry(C.byref(interface))
                interfaces[route.index]={'status':int(status)}
                if not status:
                    interfaces[route.index].update({n:int(getattr(interface,n)) for n in
                        ('admin_status','oper_status','speed','in_octets','out_octets','in_errors','out_errors','in_discards','out_discards')})
        endpoints.append(item)
    return {'state':'observed','endpoints':endpoints,'interfaces':interfaces,'identity_confirmed':False}


def helper_main(hosts):
    result=cached_network(hosts)
    encoded=json.dumps(result,allow_nan=False)
    if len(encoded.encode()) > MAX_OUTPUT:
        raise ValueError('Cache output exceeds budget')
    print(encoded)
    return 0


def bounded_snapshot(hosts):
    """Kill and reap the exact fixed helper, including a native call that hangs."""
    hosts=list(hosts)
    if len(hosts)>8:
        raise ValueError('Too many endpoints')
    for host in hosts:
        ipaddress.IPv4Address(host)
    command=([sys.executable,'--passive-cache-helper'] if getattr(sys,'frozen',False)
             else [sys.executable,str(Path(__file__).resolve())]) + hosts
    with tempfile.TemporaryFile() as output:
        child=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.DEVNULL,
                               close_fds=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),
                               env={k:v for k,v in os.environ.items() if k != 'PYINSTALLER_RESET_ENVIRONMENT'})
        deadline=time.monotonic()+BUDGET_SECONDS
        try:
            while child.poll() is None:
                if time.monotonic()>=deadline or os.fstat(output.fileno()).st_size>MAX_OUTPUT:
                    raise TimeoutError('Passive cache helper exceeded budget')
                time.sleep(.02)
            if child.returncode or os.fstat(output.fileno()).st_size>MAX_OUTPUT:
                raise ValueError('Passive cache helper failed')
            output.seek(0);result=json.loads(output.read(MAX_OUTPUT+1))
            if not isinstance(result,dict) or not isinstance(result.get('endpoints'),list) or len(result['endpoints'])>8:
                raise ValueError('Invalid cache output')
            if any(item.get('host') not in hosts for item in result['endpoints']):
                raise ValueError('Unexpected endpoint')
            return result
        finally:
            if child.poll() is None:
                if os.name == 'nt':
                    # PyInstaller onefile has a bootloader and a Python child.
                    # Kill the owned tree, not only the outer bootloader.
                    try:
                        subprocess.run(['taskkill.exe','/PID',str(child.pid),'/T','/F'],
                                       stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL,timeout=3,check=False)
                    except (OSError,subprocess.TimeoutExpired):
                        pass
                    if child.poll() is None:child.kill()
                else:child.kill()
            child.wait(timeout=5)


class PassiveIncident:
    """Local opt-in policy only; cloud activation deliberately not implemented."""
    def __init__(self, data_dir, snapshot=bounded_snapshot):
        self.root=Path(data_dir)/'passive-incidents'
        self.snapshot=snapshot
        self.scope=None;self.expires=0
        try:
            path=Path(data_dir)/'passive-incident-policy.json'
            if path.stat().st_size<=4096:
                policy=json.loads(path.read_text())
                expiry=policy['expires_at']
                if policy.get('enabled') is True and time.time()<expiry<=time.time()+72*3600:
                    scope=policy['scope']
                    if set(scope)=={'customer_id','site_id'} and all(isinstance(v,str) and v for v in scope.values()):
                        self.scope=scope;self.expires=expiry
        except (OSError,ValueError,KeyError,TypeError):
            pass
        self.registered=()
        self.jobs=queue.Queue(maxsize=16)
        self.closed=threading.Event()
        self.thread=None
        try:self._prune()
        except OSError:pass
        if self.scope:
            self.thread=threading.Thread(target=self._run,name='passive-incident',daemon=True)
            self.thread.start()

    def configure(self, configured):
        if not self.scope:return
        self.registered=tuple((str(device['id']),str(device['host'])) for room,device in configured
                              if room.get('customer_id')==self.scope['customer_id'] and device.get('site_id')==self.scope['site_id'])[:8]

    def submit(self, customer_id, site_id, device, evidence):
        if self.closed.is_set() or not self.scope or time.time()>=self.expires:return
        if (customer_id,site_id)!=(self.scope['customer_id'],self.scope['site_id']):return
        key=(str(device['id']),str(device['host']))
        if key not in self.registered:return
        # Copy only fixed scalar fields. No raw frames, credentials or exception text.
        event={k:evidence.get(k) for k in ('stage','error_type','errno','winerror','failure_count','retry_seconds')
               if isinstance(evidence.get(k),(str,int,float,type(None)))}
        event.update(device_id=key[0],host=key[1],wall_time=time.time(),monotonic=time.monotonic())
        try:self.jobs.put_nowait(event)
        except queue.Full:pass

    def _write(self, event):
        self.root.mkdir(exist_ok=True)
        self._prune()
        path=self.root/'incident-0.jsonl'
        line=json.dumps(event,allow_nan=False,separators=(',',':'))+'\n'
        if len(line.encode())>MAX_OUTPUT:return
        if path.exists() and path.stat().st_size+len(line.encode())>2*1024*1024:
            (self.root/'incident-4.jsonl').unlink(missing_ok=True)
            for i in range(3,-1,-1):
                old=self.root/f'incident-{i}.jsonl'
                if old.exists():old.replace(self.root/f'incident-{i+1}.jsonl')
        with path.open('a',encoding='utf-8') as stream:stream.write(line)

    def _prune(self):
        now=time.time()
        for path in self.root.glob('incident-*.jsonl'):
            if now-path.stat().st_mtime>72*3600:path.unlink()

    def _error(self, exc):
        try:self._write({'kind':'cache_error','error_type':type(exc).__name__,'wall_time':time.time()})
        except Exception:pass

    def _reserve_window(self):
        self.root.mkdir(exist_ok=True)
        path=self.root/'window-budget.json'
        state=read_json(path)
        if path.exists() and (not state or type(state.get('count')) is not int):return False
        day=int(time.time()//86400)
        count=state.get('count',0) if state.get('day')==day else 0
        if count>=6:return False
        atomic_json(path,{'day':day,'count':count+1})
        return True

    def _run(self):
        failures={};until=0;next_snapshot=0;errors=0;cooldown=0
        preceding=deque(maxlen=16)
        while not self.closed.is_set():
            if time.time()>=self.expires:
                try:self._prune()
                except OSError:pass
                self.closed.wait(60)
                continue
            now=time.monotonic()
            try:
                event=self.jobs.get(timeout=.5)
                if time.time()>=self.expires:continue
                preceding.append(event)
                failures[event['device_id']]=event['monotonic']
                failures={k:v for k,v in failures.items() if now-v<=60}
                triggered=False
                if now>=cooldown and (len(failures)>=3 or (event.get('failure_count') or 0)>=3):
                    cooldown=now+900
                    if self._reserve_window():
                        until=now+600;next_snapshot=now;triggered=True
                        for previous in preceding:self._write({'kind':'failure','scope':self.scope,**previous})
                        preceding.clear()
                if until>now and not triggered:self._write({'kind':'failure','scope':self.scope,**event})
                if until>now and now>=next_snapshot:
                    next_snapshot=now+60
                    hosts=list(dict.fromkeys(host for _,host in self.registered))
                    self._write({'kind':'cache','scope':self.scope,'wall_time':time.time(),'monotonic':now,
                                 'cache':self.snapshot(hosts)})
                    errors=0
            except queue.Empty:
                if until>now and now>=next_snapshot:
                    # Wake without needing another failure from a backed-off device.
                    try:
                        next_snapshot=now+60
                        self._write({'kind':'cache','scope':self.scope,'wall_time':time.time(),'monotonic':now,
                                     'cache':self.snapshot(list(dict.fromkeys(h for _,h in self.registered)))})
                        errors=0
                    except Exception as exc:errors+=1;self._error(exc)
            except Exception as exc:errors+=1;self._error(exc)
            if errors>=3:
                try:self._write({'kind':'disabled','reason':'three_consecutive_diagnostic_errors','wall_time':time.time()})
                except OSError:pass
                self.closed.set()  # Disable this optional worker, never the collector.

    def close(self):
        self.closed.set()
        if self.thread:self.thread.join(timeout=BUDGET_SECONDS+1)


if __name__=='__main__':
    raise SystemExit(helper_main(sys.argv[1:]))
