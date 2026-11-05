"""Fail-closed, explicitly bound LAN identity; never learn a binding from a sample.

MAC identifies a directly attached network adapter, not a cryptographic sensor
identity. Routed/proxy-ARP/shared-gateway arrangements require another identity
scheme and are deliberately not accepted here.
"""
from __future__ import annotations
import ctypes as C
import ipaddress
import os
import queue
import re
import socket
import threading
import time

class IdentityError(RuntimeError):
    pass

def normalize_mac(value):
    value = str(value or '').strip().replace('-', ':').lower()
    if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', value):
        raise ValueError('A six-byte MAC address is required')
    raw = bytes.fromhex(value.replace(':',''))
    if raw == bytes(6) or raw[0] & 1:
        raise ValueError('A unicast, nonzero MAC address is required')
    return value

class SockaddrInet(C.Union):
    _fields_ = [('alignment', C.c_uint32), ('raw', C.c_ubyte * 28)]

def sockaddr(ip):
    result = SockaddrInet()
    result.raw[0:2] = (socket.AF_INET).to_bytes(2, 'little')
    result.raw[4:8] = socket.inet_aton(ip)
    return result

class NeighborRow(C.Structure):
    _fields_ = [('address', SockaddrInet), ('index', C.c_uint32),
                ('luid', C.c_uint64), ('physical', C.c_ubyte * 32),
                ('length', C.c_uint32), ('state', C.c_uint32),
                ('flags', C.c_ubyte), ('reachability', C.c_uint32)]

class RouteRow(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in
        ('dest','mask','policy','next_hop','index','type','proto','age',
         'next_hop_as','metric1','metric2','metric3','metric4','metric5')]

def resolve_windows_neighbor(host):
    """Actively resolve the exact endpoint on its selected on-link interface.

    ResolveIpNetEntry2 refreshes the one neighbor entry instead of trusting a
    stale ARP cache. No subnet scan or global ARP flush is performed.
    """
    if os.name != 'nt':
        raise IdentityError('Identity verification requires Windows LAN support')
    address = ipaddress.ip_address(host)
    if address.version != 4 or address.is_loopback or not address.is_private:
        raise IdentityError('Identity verification requires a private IPv4 endpoint')
    api = C.WinDLL('iphlpapi.dll')
    route = RouteRow()
    api.GetBestRoute.argtypes = [C.c_uint32,C.c_uint32,C.POINTER(RouteRow)]
    api.GetBestRoute.restype = C.c_uint32
    destination = int.from_bytes(address.packed, 'little')
    if api.GetBestRoute(destination,0,C.byref(route)) or route.next_hop != 0 or not route.index:
        raise IdentityError('Endpoint is not directly attached; gateway MAC is not identity')
    row = NeighborRow(); row.address = sockaddr(str(address)); row.index = route.index
    api.ResolveIpNetEntry2.argtypes = [C.POINTER(NeighborRow),C.POINTER(SockaddrInet)]
    api.ResolveIpNetEntry2.restype = C.c_uint32
    error = api.ResolveIpNetEntry2(C.byref(row),None)
    if error or row.length != 6 or row.flags & 3 or row.state not in (5,6):
        raise IdentityError(f'Fresh device identity unavailable (Windows status {error})')
    return {'mac': normalize_mac(':'.join(f'{b:02x}' for b in row.physical[:6])),
            'interface': int(row.index), 'host':str(address)}

class IdentityVerifier:
    def __init__(self, resolver=resolve_windows_neighbor, timeout=4, capacity=8):
        self.resolver=resolver; self.timeout=timeout
        self.slots=threading.BoundedSemaphore(capacity)

    def observe(self, host):
        if not self.slots.acquire(blocking=False):
            raise IdentityError('Identity resolver busy; sample withheld')
        results=queue.Queue(maxsize=1)
        def run():
            try: results.put((True,self.resolver(host)))
            except Exception as exc: results.put((False,exc))
            finally:self.slots.release()
        threading.Thread(target=run,daemon=True,name='device-identity').start()
        try: ok,value=results.get(timeout=self.timeout)
        except queue.Empty: raise IdentityError('Identity resolution timed out; sample withheld')
        if not ok: raise IdentityError(str(value))
        if value.get('host') != host or not value.get('interface'):
            raise IdentityError('Identity endpoint/interface mismatch')
        return dict(value,mac=normalize_mac(value.get('mac')))

    @staticmethod
    def check(expected, before, after):
        if before != after:
            raise IdentityError('Device identity changed during sampling')
        if not expected:
            raise IdentityError('Device identity has not been explicitly bound')
        if before['mac'] != normalize_mac(expected):
            raise IdentityError('Device identity does not match the approved binding')
