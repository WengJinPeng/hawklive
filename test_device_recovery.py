"""Device-link recovery, using real local TCP sockets and an isolated SQLite DB."""
from __future__ import annotations

import socketserver
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from dashboard_server import DashboardHandler, demo_reading, init_db, record_reading
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
        self.minimum_gap = 0.0
        self.overloaded = threading.Event()
        self.request_received = threading.Event()
        self.recovery_quiet_seconds = 0.0
        self.last_connection_at = 0.0
        self.connection_times = []


class ModbusHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.connections += 1
        now = time.monotonic()
        gap = now - self.server.last_connection_at
        self.server.last_connection_at = now
        self.server.connection_times.append(now)
        if gap < self.server.recovery_quiet_seconds:
            return  # Reconnecting too early restarts this gateway's recovery timer.
        if self.server.reject_connections.is_set():
            return  # TCP accepted, then closed by the rebooting Wi-Fi module.
        broken = False
        self.request.settimeout(2)
        last_response_at = 0.0
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
                self.server.request_received.set()
                if time.monotonic() - last_response_at < self.server.minimum_gap:
                    self.server.overloaded.set()
                if self.server.overloaded.is_set():
                    continue  # Embedded bridge cannot recover until reset.
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
                last_response_at = time.monotonic()
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
    def test_one_retry_then_low_frequency_probes_and_no_cooldown_bypass(self, client_type):
        client_type.return_value.read_realtime.side_effect = TimeoutError("Wi-Fi lost")
        with patch("monitoring_service.time.monotonic", return_value=1000.0) as clock:
            for attempt, delay in enumerate([10, 60, 120, 120, 120], 1):
                self.service._poll_device(self.room, self.device)
                self.assertEqual(self.service._next_poll_at[self.device["id"]], clock.return_value + delay)
                for _ in range(3):
                    self.service._poll_device(self.room, self.device)
                    with self.assertRaisesRegex(ConnectionError, "cooling down"):
                        self.service.test_device_connection(self.device)
                self.assertEqual(client_type.call_count, attempt)
                self.assertEqual(self.service._device_failures[self.device["id"]], attempt)
                clock.return_value += delay
        self.assertNotIn(self.device["id"], self.service._device_clients)
        self.assertEqual(self.rows(), [])

    @patch("monitoring_service.Dcp8001TcpClient")
    def test_connection_test_failure_throttles_background_and_manual_reads(self, client_type):
        client_type.return_value.read_realtime.side_effect = TimeoutError("broken session")
        with patch("monitoring_service.time.monotonic", return_value=1000.0) as clock:
            with self.assertRaises(TimeoutError):
                self.service.test_device_connection(self.device)
            self.service._poll_device(self.room, self.device)
            self.assertEqual(client_type.call_count, 1)
            with self.assertRaisesRegex(ConnectionError, "cooling down"):
                self.service.test_device_connection(self.device)
            self.assertEqual(client_type.call_count, 1)
            clock.return_value += 10
            client_type.return_value.read_realtime.side_effect = None
            client_type.return_value.read_realtime.return_value = self.sample(time.time())
            self.service._poll_device(self.room, self.device)
            self.service._poll_device(self.room, self.device)
            self.assertEqual(client_type.call_count, 2)
            self.assertNotIn(self.device["id"], self.service._device_failures)
            self.assertEqual(len(self.rows()), 1)

    @patch("monitoring_service.Dcp8001TcpClient")
    def test_successful_manual_probe_unblocks_poll_and_persists_recovery(self, client_type):
        client_type.side_effect = ConnectionRefusedError("gateway unavailable")
        with patch("monitoring_service.time.monotonic", return_value=1000.0) as clock:
            self.service._poll_device(self.room, self.device)
            clock.return_value += 10
            sample = SimpleNamespace(**self.sample(time.time()))
            client_type.side_effect = None
            client_type.return_value.read_realtime.return_value = sample
            result = self.service.test_device_connection(self.device)
            self.assertEqual(result["measured_at"], sample.timestamp)
            self.service._poll_device(self.room, self.device)
            self.assertEqual(client_type.call_count, 2)
            self.assertTrue(self.service.latest[self.device["id"]]["online"])
            self.assertEqual(len(self.rows()), 1)
            self.assertNotIn(self.device["id"], self.service._device_failures)

    def test_legacy_short_cap_cannot_disable_recovery_cooldown(self):
        self.service.options.offline_backoff_max = 10
        self.assertEqual([self.service._reconnect_delay(n) for n in (1, 2, 3, 100000)],
                         [10, 60, 60, 60])

    @patch("monitoring_service.Dcp8001TcpClient")
    def test_connection_test_handler_returns_cooldown_error_then_recovers(self, client_type):
        client_type.return_value.read_realtime.side_effect = TimeoutError("Wi-Fi lost")
        handler = MagicMock()
        handler.read_json_body.return_value = self.device
        with patch("dashboard_server.MONITOR", self.service), \
                patch("dashboard_server.add_log"), \
                patch("monitoring_service.time.monotonic", return_value=1000.0) as clock:
            self.service._poll_device(self.room, self.device)
            DashboardHandler.handle_collector_device_test(handler)
            payload, status = handler.write_json.call_args.args
            self.assertEqual(status, 502)
            self.assertFalse(payload["ok"])
            self.assertIn("cooling down", payload["error"])
            self.assertEqual(client_type.call_count, 1)
            clock.return_value += 10
            client_type.return_value.read_realtime.side_effect = None
            client_type.return_value.read_realtime.return_value = SimpleNamespace(**self.sample(time.time()))
            DashboardHandler.handle_collector_device_test(handler)
            self.assertTrue(handler.write_json.call_args.args[0]["ok"])
            self.assertEqual(client_type.call_count, 2)

    @patch("monitoring_service.Dcp8001TcpClient")
    def test_outage_boundaries_are_saved_without_fabricating_missing_samples(self, client_type):
        now = time.time()
        client_type.return_value.read_realtime.side_effect = [
            self.sample(now), self.sample(now + 10), TimeoutError("Wi-Fi lost"),
            TimeoutError("still offline"), self.sample(now + 25),
        ]
        for _ in range(5):
            self.service._next_poll_at[self.device["id"]] = 0
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
        self.service._reconnect_delay = lambda _count: 0.2  # Accelerate only the fixture clock.
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

    def test_slow_valid_responses_do_not_force_a_reconnect(self):
        gateway = self.start_gateway()
        gateway.response_delay = 0.03
        with Dcp8001TcpClient("127.0.0.1", gateway.server_address[1], timeout=0.1, connect_settle=0) as client:
            for _ in range(2):
                self.assertEqual(client.read_realtime().cleanliness_code, 7)
        self.assertEqual(gateway.connections, 1)

    def test_real_tcp_gateway_requiring_quiet_time_recovers_and_reuses_session(self):
        gateway = self.start_gateway()
        gateway.recovery_quiet_seconds = 0.4
        gateway.last_connection_at = time.monotonic()
        self.device.update(host="127.0.0.1", tcpPort=gateway.server_address[1])
        self.service.configuration = lambda: [self.room]
        self.service.options.poll_seconds = 0.1
        self.service.options.device_timeout = 0.2
        # Run the production 10/60/120 policy on a 100x faster test timescale.
        production_delay = self.service._reconnect_delay
        self.service._reconnect_delay = lambda count: production_delay(count) / 100
        recovered = threading.Event()
        self.service.add_log = lambda _level, event, _msg: (
            recovered.set() if event == "device_recovered" else None
        )
        factory = lambda **kwargs: Dcp8001TcpClient(**kwargs, connect_settle=0.02)
        with patch("monitoring_service.Dcp8001TcpClient", side_effect=factory):
            self.service.start()
            try:
                self.assertTrue(recovered.wait(4), "gateway never receives enough quiet time to recover")
                self.assertEqual(gateway.connections, 3)
                self.assertGreaterEqual(gateway.connection_times[2] - gateway.connection_times[1], 0.59)
                self.service._poll_device(self.room, self.device)
                self.assertEqual(gateway.connections, 3, "healthy connection was not reused")
                self.assertTrue(self.service.latest[self.device["id"]]["online"])
                self.assertGreaterEqual(len(self.rows()), 1)
                self.assertEqual(set(gateway.functions), {3})
            finally:
                self.service.stop()

    def test_back_to_back_requests_do_not_overrun_gateway(self):
        gateway = self.start_gateway()
        gateway.minimum_gap = 0.035
        with Dcp8001TcpClient("127.0.0.1", gateway.server_address[1], timeout=0.2, connect_settle=0) as client:
            for _ in range(3):
                self.assertEqual(client.read_realtime().cleanliness_code, 7)
        self.assertFalse(gateway.overloaded.is_set())
        self.assertEqual(gateway.connections, 1)

    @patch("monitoring_service.Dcp8001TcpClient")
    def test_storage_failure_does_not_disconnect_healthy_device(self, client_type):
        client_type.return_value.read_realtime.return_value = self.sample(time.time())
        original_record = self.service.record_reading
        self.service.record_reading = MagicMock(side_effect=OSError("disk temporarily unavailable"))
        self.service._poll_device(self.room, self.device)
        client_type.return_value.close.assert_not_called()
        self.assertTrue(self.service.latest[self.device["id"]]["online"])
        self.assertNotIn(self.device["id"], self.service._device_failures)
        self.assertTrue(any(event == "device_sample_save_failed" for _, event, _ in self.logs))
        self.service.record_reading = original_record
        self.service._poll_device(self.room, self.device)
        self.assertEqual(client_type.call_count, 1)
        self.assertEqual(len(self.rows()), 1)

    @patch("monitoring_service.Dcp8001TcpClient")
    def test_alarm_database_failure_keeps_session_and_does_not_save_false_normal(self, client_type):
        client_type.return_value.read_realtime.return_value = self.sample(time.time())
        with patch.object(self.service, "_evaluate_alarms", side_effect=OSError("database locked")):
            self.service._poll_device(self.room, self.device)
        client_type.return_value.close.assert_not_called()
        self.assertNotIn(self.device["id"], self.service._device_failures)
        self.assertNotIn(self.device["id"], self.service.latest)
        self.assertTrue(any(event == "device_sample_processing_failed" for _, event, _ in self.logs))
        self.assertEqual(self.rows(), [])
        self.service._poll_device(self.room, self.device)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(client_type.call_count, 1)

    @patch("monitoring_service.Dcp8001TcpClient")
    def test_failed_reader_evicts_session_before_another_reader_enters(self, client_type):
        failed_client, replacement = MagicMock(), MagicMock()
        failed_client.read_realtime.side_effect = TimeoutError("broken session")
        client_type.side_effect = [failed_client, replacement]
        released, resume = threading.Event(), threading.Event()
        device_id = self.device["id"]

        class HandoffLock:
            def __init__(self):
                self.lock = threading.RLock()
                self.depth = 0

            def __enter__(self):
                self.lock.acquire()
                self.depth += 1

            def __exit__(self, *_args):
                self.depth -= 1
                handoff = self.depth == 0 and threading.current_thread().name == "failed-reader"
                self.lock.release()
                if handoff:
                    released.set()
                    resume.wait(3)

        lock = HandoffLock()
        self.service._device_io_locks[device_id] = lock
        # Exercise both background and connection-test error paths.
        for connection_test in (False, True):
            with self.subTest(connection_test=connection_test):
                client_type.reset_mock()
                client_type.side_effect = [failed_client, replacement]
                released.clear()
                resume.clear()
                errors = []

                def read():
                    try:
                        if connection_test:
                            self.service.test_device_connection(self.device)
                        else:
                            self.service._poll_device(self.room, self.device)
                    except TimeoutError:
                        errors.append("expected")

                worker = threading.Thread(target=read, name="failed-reader")
                worker.start()
                try:
                    self.assertTrue(released.wait(2))
                    with lock:
                        with self.assertRaisesRegex(ConnectionError, "cooling down"):
                            self.service._client_for_device(self.device)
                        self.assertEqual(client_type.call_count, 1)
                        self.service._next_poll_at[device_id] = 0
                        self.assertIs(self.service._client_for_device(self.device), replacement)
                    resume.set()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                    replacement.close.assert_not_called()
                    self.assertIs(self.service._device_clients[device_id][1], replacement)
                    self.assertEqual(errors, ["expected"] if connection_test else [])
                finally:
                    resume.set()
                    worker.join(3)
                    self.service._close_device_client(device_id)
                    self.service._device_failures.clear()
                    self.service._next_poll_at.clear()
                    replacement.reset_mock()

    def test_stop_interrupts_stalled_socket_without_waiting_for_sample_budget(self):
        gateway = self.start_gateway()
        gateway.drop_link.set()
        self.device.update(host="127.0.0.1", tcpPort=gateway.server_address[1])
        self.service.configuration = lambda: [self.room]
        self.service.options.device_timeout = 5
        factory = lambda **kwargs: Dcp8001TcpClient(**kwargs, connect_settle=0)
        with patch("monitoring_service.Dcp8001TcpClient", side_effect=factory):
            self.service.start()
            try:
                self.assertTrue(gateway.request_received.wait(2))
                started = time.monotonic()
                self.service.stop()
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertFalse(self.service._thread.is_alive())
                self.assertEqual(self.service._device_clients, {})
            finally:
                self.service.stop()

    def test_peer_closing_during_initial_settle_recovers_automatically(self):
        gateway = self.start_gateway()
        gateway.reject_connections.set()
        self.device.update(host="127.0.0.1", tcpPort=gateway.server_address[1])
        self.service.configuration = lambda: [self.room]
        self.service.options.poll_seconds = 0.1
        self.service.options.device_timeout = 0.2
        self.service._reconnect_delay = lambda _count: 0.2  # Accelerate only the fixture clock.
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
