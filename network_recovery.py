"""Read-only adapter diagnosis and bounded unattended reconnect scheduling."""
from __future__ import annotations
import ipaddress
import threading
import time

_lock = threading.Lock()
_cached_at = float('-inf')
_cached = None


def active_adapters():
    global _cached_at, _cached
    with _lock:
        now = time.monotonic()
        if now - _cached_at >= 5:
            try:
                import psutil  # Missing inventory support means unknown, not disconnected.
                from device_discovery import local_private_networks
                _cached = local_private_networks()
            except Exception:
                _cached = None
            _cached_at = now
        return None if _cached is None else [dict(item) for item in _cached]


def matching_sources(host, adapters):
    try:
        address = ipaddress.ip_address(host)
        if address.version != 4 or address.is_loopback or address.is_link_local:
            return []
        return sorted({item['address'] for item in adapters or []
                       if address in ipaddress.ip_network(item['adapter_cidr'])})
    except (ValueError, KeyError, TypeError):
        return []


def device_source_address(host):
    # Never guess between overlapping adapters or alter the OS route table.
    sources = matching_sources(host, active_adapters())
    return sources[0] if len(sources) == 1 else None


class NetworkRecovery:
    def __init__(self):
        self.signature = None
        self.adapters = None
        self.checked_at = float('-inf')
        self.last_reprobe = float('-inf')
        self.changed_at = None
        self.pending_reprobe = False

    def check(self, now, wall_time):
        if now - self.checked_at < 5:
            return False
        self.checked_at = now
        self.adapters = active_adapters()
        if self.adapters is None:
            return False
        signature = tuple(sorted((item['name'],item['address'],item['adapter_cidr']) for item in self.adapters))
        changed = self.signature is not None and signature != self.signature
        self.signature = signature
        if changed:
            self.changed_at = wall_time
            self.pending_reprobe = True
        if self.pending_reprobe and now - self.last_reprobe >= 60:
            self.last_reprobe = now
            self.pending_reprobe = False
            return True
        return False

    def state(self, hosts, failed_count):
        if not hosts or self.adapters is None:
            return 'unknown'
        if not failed_count:
            return 'healthy'
        if not self.adapters:
            return 'lan_unavailable'
        if all(not matching_sources(host,self.adapters) for host in hosts):
            # Routed subnets can be valid; this does not prove there is no route.
            return 'no_direct_lan'
        return 'reconnecting'
