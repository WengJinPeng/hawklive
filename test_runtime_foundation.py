from __future__ import annotations

import socket
import sqlite3
from contextlib import closing
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import dashboard_server
from device_discovery import (
    automatic_scan_networks,
    local_private_networks,
    probe_modbus_host,
)
from runtime_paths import migrate_legacy_runtime_data, resolve_data_dir, runtime_paths
from single_instance import AlreadyRunningError, SingleInstanceLock


class RuntimePathTests(unittest.TestCase):
    def test_windows_defaults_to_program_data(self) -> None:
        result = resolve_data_dir(
            environ={"ProgramData": r"C:\CompanyData"},
            platform_name="nt",
            app_directory=Path("/source"),
        )
        self.assertEqual(result, Path(r"C:\CompanyData") / "HawkHive" / "DCP8001")

    def test_explicit_data_directory_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = resolve_data_dir(
                environ={"DCP_DATA_DIR": directory, "ProgramData": r"C:\Ignored"},
                platform_name="nt",
            )
            self.assertEqual(result, Path(directory).resolve())

    def test_runtime_directories_are_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = runtime_paths(environ={"DCP_DATA_DIR": directory}).ensure()
            self.assertTrue(paths.logs_dir.is_dir())
            self.assertTrue(paths.backups_dir.is_dir())

    def test_legacy_database_and_settings_are_migrated_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = root / "data"
            with closing(sqlite3.connect(source / "dashboard_data.sqlite3")) as db, db:
                db.execute("CREATE TABLE proof(value TEXT)")
                db.execute("INSERT INTO proof(value) VALUES('legacy')")
            (source / "collector_settings.json").write_text("{}", encoding="utf-8")
            paths = runtime_paths(environ={"DCP_DATA_DIR": str(target)})

            migrated = migrate_legacy_runtime_data(paths, app_directory=source)
            self.assertEqual(migrated, ["dashboard_data.sqlite3", "collector_settings.json"])
            with closing(sqlite3.connect(paths.database)) as db, db:
                self.assertEqual(db.execute("SELECT value FROM proof").fetchone()[0], "legacy")
            paths.collector_settings.write_text('{"current":true}', encoding="utf-8")
            self.assertEqual(migrate_legacy_runtime_data(paths, app_directory=source), [])
            self.assertEqual(paths.collector_settings.read_text(), '{"current":true}')


class SingleInstanceTests(unittest.TestCase):
    def test_second_process_lock_is_rejected_until_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collector.lock"
            first = SingleInstanceLock(path)
            second = SingleInstanceLock(path)
            first.acquire()
            try:
                with self.assertRaises(AlreadyRunningError):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()

    def test_collector_can_be_stopped_through_service_shutdown_hook(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = runtime_paths(environ={"DCP_DATA_DIR": directory})

            with (
                patch.multiple(
                    dashboard_server,
                    RUNTIME_PATHS=paths,
                    DB_PATH=paths.database,
                    COLLECTOR_SETTINGS_PATH=paths.collector_settings,
                ),
                patch.dict("os.environ", {"DCP_DEMO_MODE": "1"}),
                ThreadPoolExecutor(max_workers=1) as executor,
            ):
                future = executor.submit(dashboard_server.run_collector, "127.0.0.1", 0)
                deadline = time.time() + 5
                while dashboard_server.HTTP_SERVER is None and time.time() < deadline:
                    time.sleep(0.02)
                self.assertIsNotNone(dashboard_server.HTTP_SERVER)
                dashboard_server.request_shutdown()
                # Production shutdown waits for an in-flight device operation for
                # up to the configured timeout before closing shared resources.
                self.assertEqual(future.result(timeout=20), 0)


class NetworkAdapterTests(unittest.TestCase):
    def test_private_active_adapters_are_listed_and_large_ranges_are_bounded(self) -> None:
        fake_psutil = SimpleNamespace(
            net_if_stats=lambda: {
                "Ethernet": SimpleNamespace(isup=True),
                "Offline": SimpleNamespace(isup=False),
            },
            net_if_addrs=lambda: {
                "Ethernet": [SimpleNamespace(
                    family=socket.AF_INET, address="10.20.30.40", netmask="255.255.0.0"
                )],
                "Offline": [SimpleNamespace(
                    family=socket.AF_INET, address="192.168.5.10", netmask="255.255.255.0"
                )],
            },
        )
        with patch.dict("sys.modules", {"psutil": fake_psutil}):
            results = local_private_networks()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["adapter_cidr"], "10.20.0.0/16")
        self.assertEqual(results[0]["scan_cidr"], "10.20.30.0/24")
        self.assertTrue(results[0]["partial"])

    @patch("device_discovery.local_private_networks")
    def test_automatic_scan_uses_every_active_private_adapter(self, local_networks) -> None:
        local_networks.return_value = [
            {"scan_cidr": "192.168.2.0/24"},
            {"scan_cidr": "10.20.30.0/24"},
            {"scan_cidr": "192.168.2.0/24"},
        ]

        self.assertEqual(
            [str(item) for item in automatic_scan_networks("https://example.invalid")],
            ["192.168.2.0/24", "10.20.30.0/24"],
        )

    @patch("device_discovery.secrets.randbelow", return_value=0x4000)
    @patch("device_discovery.socket.create_connection")
    def test_probe_requires_realtime_and_firmware_registers_for_protocol_match(
        self, connect, _random_transaction
    ) -> None:
        class FakeSocket:
            def __init__(self) -> None:
                self.requests: list[bytes] = []
                self.responses = bytearray(
                    # A complete response left behind by an older connection.
                    b"\x12\x34\x00\x00\x00\x05\x01\x03\x02\x00\x09"
                )

            def settimeout(self, _timeout: float) -> None:
                pass

            def sendall(self, request: bytes) -> None:
                self.requests.append(request)
                transaction_id = request[0:2]
                start = int.from_bytes(request[8:10], "big")
                quantity = int.from_bytes(request[10:12], "big")
                values = {0: [5, 0], 36: [0x0102], 133: [1]}[start]
                data = b"".join(value.to_bytes(2, "big") for value in values)
                self.responses.extend(
                    transaction_id
                    + b"\x00\x00"
                    + (len(data) + 3).to_bytes(2, "big")
                    + b"\x01\x03"
                    + bytes((len(data),))
                    + data
                )

            def recv(self, length: int) -> bytes:
                result = bytes(self.responses[:length])
                del self.responses[:length]
                return result

            def close(self) -> None:
                pass

        fake_socket = FakeSocket()
        connect.return_value = fake_socket
        result = probe_modbus_host("192.168.1.88")
        self.assertIsNotNone(result)
        self.assertTrue(result["verified"])
        self.assertTrue(result["protocol_compatible"])
        self.assertFalse(result["identity_verified"])
        self.assertEqual(result["firmware_raw"], 0x0102)
        self.assertEqual(result["particle_unit_code"], 1)
        self.assertEqual(result["particle_unit_label"], "PCS/28.3L")
        self.assertTrue(result["unit_supported"])
        self.assertEqual(int.from_bytes(fake_socket.requests[0][10:12], "big"), 2)


if __name__ == "__main__":
    unittest.main()
