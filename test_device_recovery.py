"""Device-link recovery, using real local TCP sockets and an isolated SQLite DB."""
from __future__ import annotations

import socketserver
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dashboard_server import demo_reading, init_db, record_reading
from dcp8001_collector import Dcp8001TcpClient
from monitoring_service import MonitorOptions, MonitoringService


class ModbusFixture(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), ModbusHandler)
        self.drop_link = threading.Event()
        self.reject_connections = threading.Event()
        self.old_socket_closed = threading.Event()
        self.connections = 0
        self.response_delay = 0
        self.functions = []


class ModbusHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.connections += 1
        if self.server.reject_connections.is_set():
            return  # TCP accepted, then closed by the rebooting Wi-Fi module.
        broken = False
        self.request.settimeout(2)
        try:
            while True:
                frame = bytearray()
                while len(frame) < 12:
                    chunk = self.request.recv(12 - len(frame))
                    if not chunk:
                        if broken:
                            self.server.old_socket_closed.set()
                        return
                    frame.extend(chunk)
                self.server.functions.append(frame[7])
                broken = broken or self.server.drop_link.is_set()
                if broken:
                    continue  # Wi-Fi gateway keeps TCP open but never replies.
                start, quantity = struct.unpack(">HH", frame[8:12])
                registers = [0] * quantity
                if start == 133:
                    registers = [1]
                elif start == 2:
                    registers = [self.server.connections, 0]
                elif start == 14:
                    registers = [0, 0x41B0]  # 22 C, low word first
                elif start == 16:
                    registers = [0, 0x4248]  # 50 %RH
                elif start == 24:
                    registers = [0, 7]
                body = bytes([3, quantity * 2]) + struct.pack(">" + "H" * quantity, *registers)
                if self.server.response_delay:
                    time.sleep(self.server.response_delay)
                self.request.sendall(frame[:4] + struct.pack(">H", len(body) + 1) + frame[6:7] + body)
        except OSError:
            pass


class DeviceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "recovery.sqlite3"
        self.db_patch = patch("dashboard_server.DB_PATH", self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        init_db()
        self.logs = []
        self.service = MonitoringService(
            self.db_path, lambda name: demo_reading(name), record_reading,
            lambda *args: self.logs.append(args),
            MonitorOptions(demo=False, poll_seconds=10, record_seconds=120),
        )
        self.service.init_schema()
        self.addCleanup(self.service.stop)
        self.room = self.service.configuration()[0]
        self.service.create_device("customer-001", self.room["id"], "Fixture", "192.168.2.30", 502, 1)
        self.room = self.service.configuration()[0]
        self.device = self.room["devices"][0]

    def sample(self, timestamp):
        reading = demo_reading("Fixture")
        reading["timestamp"] = timestamp
        reading["particles"]["pm_0_5_um"] = 500
        reading["environment"].update(temperature=22, humidity=50)
        return reading

    def rows(self):
        with self.service.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM readings ORDER BY timestamp")]

    @patch("monitoring_service.Dcp8001TcpClient")
    def test_short_disconnect_retries_quickly_even_after_many_failures(self, client_type):
        client_type.return_value.read_realtime.side_effect = TimeoutError("Wi-Fi lost")
        for attempt in range(8):
            self.service._poll_device(self.room, self.device)
            retry = self.service._next_poll_at[self.device["id"]] - time.monotonic()
            self.assertLessEqual(retry, 2 if attempt == 0 else 10)
        self.assertEqual(client_type.call_count, 8)
        self.assertNotIn(self.device["id"], self.service._device_clients)
        self.assertEqual(self.rows(), [])

    @patch("monitoring_service.Dcp8001TcpClient")
    def test_outage_boundaries_are_saved_without_fabricating_missing_samples(self, client_type):
        now = time.time()
        client_type.return_value.read_realtime.side_effect = [
            self.sample(now), self.sample(now + 10), TimeoutError("Wi-Fi lost"),
            TimeoutError("still offline"), self.sample(now + 25),
        ]
        for _ in range(5):
            self.service._poll_device(self.room, self.device)
        rows = self.rows()
        self.assertEqual([row["timestamp"] for row in rows], [now, now + 10, now + 25])
        self.assertTrue(all(row["source"] == "device" and row["synced_at"] is None for row in rows))
        self.assertEqual(len({row["record_uuid"] for row in rows}), 3)
        self.assertTrue(self.service.latest[self.device["id"]]["online"])
        self.assertNotIn(self.device["id"], self.service._device_failures)

    def test_slow_device_does_not_hold_healthy_device_in_same_batch(self):
        other = {**self.device, "id": "slow-device"}
        self.service.configuration = lambda: [{**self.room, "devices": [other, self.device]}]
        self.service.options.poll_workers = 2
        slow_started, release, healthy_twice = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        counts = {}

        def poll(_room, device):
            key = device["id"]
            counts[key] = counts.get(key, 0) + 1
            if key == "slow-device":
                slow_started.set()
                release.wait(3)
            elif counts[key] == 2:
                healthy_twice.set()
            with self.service._lock:
                self.service._next_poll_at[key] = time.monotonic() + 0.1

        self.service._poll_device = poll
        self.service.start()
        try:
            self.assertTrue(slow_started.wait(1))
            self.assertTrue(healthy_twice.wait(1.5), "healthy device waits for failed device's batch")
            self.assertEqual(counts["slow-device"], 1, "do not open concurrent sessions for one device")
        finally:
            release.set()

    def test_temporary_configuration_read_failure_does_not_kill_monitor(self):
        checked = threading.Event()
        self.service.configuration = MagicMock(side_effect=[OSError("temporary read failure"), [self.room]])
        self.service._poll_device = lambda *_: (checked.set(), self.service._stop.set())
        self.service.start()
        self.assertTrue(checked.wait(2.5))

    def test_disabled_registration_releases_cached_gateway_session(self):
        closed = threading.Event()
        client = MagicMock()
        client.close.side_effect = closed.set
        self.service._device_clients[self.device["id"]] = (("192.168.2.30", 502, 1), client)
        self.service.configuration = lambda: [{**self.room, "devices": []}]
        self.service.start()
        self.assertTrue(closed.wait(1), "deleted registration keeps the gateway's only session occupied")
        self.assertNotIn(self.device["id"], self.service._device_clients)

    def start_gateway(self):
        gateway = ModbusFixture()
        thread = threading.Thread(target=gateway.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(gateway.server_close)
        self.addCleanup(gateway.shutdown)
        return gateway

    def test_real_tcp_half_open_link_recovers_without_restart(self):
        gateway = self.start_gateway()
        self.device.update(host="127.0.0.1", tcpPort=gateway.server_address[1])
        self.service.configuration = lambda: [self.room]
        self.service.options.poll_seconds = 0.1
        self.service.options.device_timeout = 0.15
        self.service.options.offline_backoff_max = 0.2
        first, failed, recovered = threading.Event(), threading.Event(), threading.Event()

        def log(level, event, message):
            self.logs.append((level, event, message))
            if event == "device_connected": first.set()
            if event == "device_offline": failed.set()
            if event == "device_recovered": recovered.set()

        self.service.add_log = log
        factory = lambda **kwargs: Dcp8001TcpClient(**kwargs, connect_settle=0)
        with patch("monitoring_service.Dcp8001TcpClient", side_effect=factory):
            self.service.start()
            try:
                self.assertTrue(first.wait(2))
                monitor_thread = self.service._thread
                gateway.drop_link.set()
                self.assertTrue(failed.wait(2))
                self.assertTrue(gateway.old_socket_closed.wait(1))
                gateway.drop_link.clear()
                self.assertTrue(recovered.wait(3))
                self.assertIs(self.service._thread, monitor_thread)
            finally:
                self.service.stop()
        self.assertGreaterEqual(gateway.connections, 2)
        self.assertEqual(set(gateway.functions), {3}, "recovery must never restart hardware or write registers")
        rows = self.rows()
        self.assertGreaterEqual(len(rows), 2, "recovered sample must be durable before the 120-second interval")
        self.assertTrue(all(row["synced_at"] is None for row in rows))

    def test_whole_read_has_deadline_even_if_each_register_replies(self):
        gateway = self.start_gateway()
        gateway.response_delay = 0.03
        with Dcp8001TcpClient("127.0.0.1", gateway.server_address[1], timeout=0.1, connect_settle=0) as client:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                client.read_realtime()
            self.assertLess(time.monotonic() - started, 0.35)

    def test_peer_closing_during_initial_settle_recovers_automatically(self):
        gateway = self.start_gateway()
        gateway.reject_connections.set()
        self.device.update(host="127.0.0.1", tcpPort=gateway.server_address[1])
        self.service.configuration = lambda: [self.room]
        self.service.options.poll_seconds = 0.1
        self.service.options.device_timeout = 0.2
        self.service.options.offline_backoff_max = 0.2
        failed, recovered = threading.Event(), threading.Event()

        def log(level, event, message):
            self.logs.append((level, event, message))
            if event == "device_offline": failed.set()
            if event == "device_recovered": recovered.set()

        self.service.add_log = log
        factory = lambda **kwargs: Dcp8001TcpClient(**kwargs, connect_settle=0.05)
        with patch("monitoring_service.Dcp8001TcpClient", side_effect=factory):
            self.service.start()
            try:
                self.assertTrue(failed.wait(2))
                self.assertIn("Socket closed while waiting for initial Modbus TCP data", self.logs[-1][2])
                self.assertEqual(self.rows(), [])
                gateway.reject_connections.clear()
                self.assertTrue(recovered.wait(2))
            finally:
                self.service.stop()
        self.assertTrue(self.service.latest[self.device["id"]]["online"])
        self.assertGreaterEqual(len(self.rows()), 1)
        self.assertEqual(set(gateway.functions), {3})


if __name__ == "__main__":
    unittest.main()
