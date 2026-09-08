from __future__ import annotations

import ipaddress
import secrets
import select
import socket
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from dcp8001_collector import PARTICLE_UNIT_LABELS, REQUIRED_PARTICLE_UNIT_CODE

PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
MAX_SCAN_ADDRESSES = 254


def local_private_networks() -> list[dict[str, Any]]:
    """List active private IPv4 adapters without relying on locale-specific ipconfig output."""
    try:
        import psutil
    except ImportError:
        return []

    statistics = psutil.net_if_stats()
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for name, addresses in psutil.net_if_addrs().items():
        status = statistics.get(name)
        if status is not None and not status.isup:
            continue
        for item in addresses:
            if item.family != socket.AF_INET or not item.netmask:
                continue
            try:
                address = ipaddress.ip_address(item.address)
                network = ipaddress.ip_network(f"{item.address}/{item.netmask}", strict=False)
            except ValueError:
                continue
            if (
                address.version != 4 or address.is_loopback or address.is_link_local
                or not any(address in allowed for allowed in PRIVATE_NETWORKS)
            ):
                continue
            # Scanning large enterprise ranges blindly is disruptive. Suggest only the
            # local /24 while retaining the real adapter range for operator visibility.
            scan_network = network if network.prefixlen >= 24 else ipaddress.ip_network(
                f"{address}/24", strict=False
            )
            key = (name, str(scan_network))
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "name": name,
                "address": str(address),
                "adapter_cidr": str(network),
                "scan_cidr": str(scan_network),
                "partial": scan_network != network,
            })
    results.sort(key=lambda item: (int(ipaddress.ip_address(item["address"])), item["name"]))
    return results


def validated_scan_network(value: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError as exc:
        raise ValueError("Scan network must be a valid IPv4 CIDR, for example 192.168.1.0/24") from exc
    if network.version != 4 or not any(network.subnet_of(allowed) for allowed in PRIVATE_NETWORKS):
        raise ValueError("Scan network must be inside a private IPv4 range")
    if network.num_addresses - 2 > MAX_SCAN_ADDRESSES:
        raise ValueError("Scan network may contain at most 254 usable addresses")
    return network


def automatic_scan_network(base_url: str) -> ipaddress.IPv4Network:
    return automatic_scan_networks(base_url)[0]


def automatic_scan_networks(base_url: str) -> list[ipaddress.IPv4Network]:
    """Return every bounded private network attached to the collector.

    The cloud-route fallback exists for minimal source installs without psutil.
    Packaged Windows builds include psutil and therefore scan all active private
    adapters rather than silently choosing a stale configured-device subnet.
    """
    adapters = local_private_networks()
    networks: list[ipaddress.IPv4Network] = []
    seen: set[str] = set()
    for adapter in adapters:
        network = validated_scan_network(str(adapter["scan_cidr"]))
        if str(network) not in seen:
            networks.append(network)
            seen.add(str(network))
    if networks:
        return networks
    parsed = urllib.parse.urlparse(base_url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Cloud URL does not contain a hostname")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    last_error: OSError | None = None
    for family, socktype, protocol, _canonname, sockaddr in socket.getaddrinfo(
        hostname, port, family=socket.AF_INET, type=socket.SOCK_DGRAM
    ):
        sock = socket.socket(family, socktype, protocol)
        try:
            sock.connect(sockaddr)
            address = ipaddress.ip_address(sock.getsockname()[0])
            if address.version == 4 and any(address in network for network in PRIVATE_NETWORKS):
                return [validated_scan_network(f"{address}/24")]
        except OSError as exc:
            last_error = exc
        finally:
            sock.close()
    if last_error:
        raise ValueError(f"Could not determine the collector's local network: {last_error}")
    raise ValueError("The collector is not connected through a private IPv4 network")


def _recv_exact(sock: socket.socket, length: int, deadline: float | None = None) -> bytes:
    data = bytearray()
    while len(data) < length:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for Modbus discovery response")
            sock.settimeout(remaining)
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("Socket closed while reading Modbus response")
        data.extend(chunk)
    return bytes(data)


def _discard_initial_data(sock: socket.socket, settle: float) -> None:
    """Discard responses queued by a previous closed client before probing."""
    deadline = time.monotonic() + max(0.0, settle)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            ready, _, _ = select.select([sock], [], [], remaining)
        except (OSError, TypeError, ValueError):
            # Socket doubles used by unit tests need not expose a file descriptor.
            return
        if not ready:
            return
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Socket closed while draining stale Modbus response")


def _read_holding_registers(
    sock: socket.socket,
    transaction_id: int,
    slave: int,
    start: int,
    quantity: int,
    timeout: float,
) -> bytes | None:
    pdu = bytes((0x03,)) + start.to_bytes(2, "big") + quantity.to_bytes(2, "big")
    request = (
        transaction_id.to_bytes(2, "big") + b"\x00\x00" + (len(pdu) + 1).to_bytes(2, "big")
        + bytes((slave,)) + pdu
    )
    sock.sendall(request)
    deadline = time.monotonic() + timeout
    while True:
        header = _recv_exact(sock, 7, deadline)
        length = int.from_bytes(header[4:6], "big")
        if not 2 <= length <= 254:
            return None
        body = _recv_exact(sock, length - 1, deadline)
        # Some embedded TCP-to-serial gateways deliver a response left behind by
        # a previous, already closed connection. Ignore complete stale MBAP frames
        # and accept only the transaction created for this probe.
        if int.from_bytes(header[0:2], "big") != transaction_id:
            continue
        if (
            int.from_bytes(header[2:4], "big") != 0
            or header[6] != slave
            or len(body) != 2 + quantity * 2
            or body[0] != 0x03
            or body[1] != quantity * 2
        ):
            return None
        return body[2:]


def probe_modbus_host(
    host: str, tcp_port: int = 502, slave: int = 1, timeout: float = 0.25
) -> dict[str, Any] | None:
    started = time.monotonic()
    sock: socket.socket | None = None
    connected = False
    verified = False
    protocol_compatible = False
    firmware_raw: int | None = None
    particle_unit_code: int | None = None
    particle_unit_label: str | None = None
    unit_supported = False
    try:
        sock = socket.create_connection((host, tcp_port), timeout=timeout)
        connected = True
        sock.settimeout(timeout)
        _discard_initial_data(sock, min(timeout, 0.25))
        transaction_id = secrets.randbelow(0xFFFC) + 1
        verified = _read_holding_registers(
            # Register 0 is the low word of the 0.3 um U32 value. The field
            # occupies registers 0-1; the real DPC8001-G rejects a half-field
            # quantity of one even though the generic protocol text says a
            # request may contain at least one holding register.
            sock, transaction_id, slave, 0, 2, timeout
        ) is not None
        if verified:
            transaction_id = (transaction_id + 1) & 0xFFFF
            firmware = _read_holding_registers(
                sock, transaction_id, slave, 36, 1, timeout
            )
            protocol_compatible = firmware is not None
            if firmware is not None:
                firmware_raw = int.from_bytes(firmware, "big")
                transaction_id = (transaction_id + 1) & 0xFFFF
                unit = _read_holding_registers(
                    sock, transaction_id, slave, 133, 1, timeout
                )
                if unit is not None:
                    particle_unit_code = int.from_bytes(unit, "big")
                    particle_unit_label = PARTICLE_UNIT_LABELS.get(
                        particle_unit_code, f"UNKNOWN({particle_unit_code})"
                    )
                    unit_supported = particle_unit_code == REQUIRED_PARTICLE_UNIT_CODE
    except (OSError, ValueError, ConnectionError):
        if not connected:
            return None
    finally:
        if sock is not None:
            sock.close()
    return {
        "host": host,
        "tcp_port": tcp_port,
        "slave": slave,
        # A target-protocol response is not yet safe to ingest when the particle
        # unit would make the configured particles/ft3 thresholds invalid.
        "verified": protocol_compatible and unit_supported,
        "modbus_responded": verified,
        "protocol_compatible": protocol_compatible,
        "identity_verified": False,
        "firmware_raw": firmware_raw,
        "particle_unit_code": particle_unit_code,
        "particle_unit_label": particle_unit_label,
        "unit_supported": unit_supported,
        "latency_ms": max(1, round((time.monotonic() - started) * 1000)),
    }


def scan_modbus_devices(
    requested_cidr: str | None,
    tcp_port: int,
    base_url: str,
    timeout: float = 0.25,
    max_workers: int = 32,
    excluded_hosts: set[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    networks, results = scan_modbus_networks(
        requested_cidr,
        tcp_port,
        base_url,
        timeout=timeout,
        max_workers=max_workers,
        excluded_hosts=excluded_hosts,
    )
    return networks[0], results


def scan_modbus_networks(
    requested_cidr: str | None,
    tcp_port: int,
    base_url: str,
    timeout: float = 0.25,
    max_workers: int = 32,
    excluded_hosts: set[str] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    networks = (
        [validated_scan_network(requested_cidr)]
        if requested_cidr else automatic_scan_networks(base_url)
    )
    excluded_addresses: set[ipaddress.IPv4Address] = set()
    for value in excluded_hosts or set():
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if address.version == 4:
            excluded_addresses.add(address)
    workers = max(1, min(max_workers, 64, MAX_SCAN_ADDRESSES))
    results: list[dict[str, Any]] = []
    for network in networks:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="device-discovery") as executor:
            futures = {
                executor.submit(probe_modbus_host, str(host), tcp_port, 1, timeout): host
                for host in network.hosts()
                if host not in excluded_addresses
            }
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    results.append(result)
    results.sort(key=lambda item: int(ipaddress.ip_address(str(item["host"]))))
    return [str(network) for network in networks], results
