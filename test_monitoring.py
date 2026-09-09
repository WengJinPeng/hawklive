from __future__ import annotations

import ipaddress
import json
import os
import sqlite3
from contextlib import closing
import tempfile
import threading
import time
import unittest
import urllib.error
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID

from alarm_config import normalise_alarm_thresholds
from auth_service import AuthenticationError, AuthService
from backup_scheduler import backup_exists_for_date, next_backup_time, parse_backup_time
from cloud_backup import (
    monthly_backup_exists,
    prune_daily_backups,
    secure_directory,
    secure_file,
)
from cloud_sync import CloudSyncService
from dashboard_server import (
    connect_db,
    demo_reading,
    init_db,
    list_readings,
    list_trend_readings,
    workbook_xlsx,
)
from device_discovery import scan_modbus_devices, scan_modbus_networks
from monitoring_service import MonitoringService, MonitorOptions
from report_i18n import normalize_report_locale, report_alarm_details, report_status


class DashboardDatabaseTests(unittest.TestCase):
    def test_demo_flow_matches_documented_2_83_litre_per_minute_nominal(self) -> None:
        values = [demo_reading("Device 1")["environment"]["flow"] for _ in range(20)]
        self.assertTrue(all(2.7 < value < 3.0 for value in values))

    @patch("device_discovery.probe_modbus_host")
    def test_network_scan_skips_configured_device_hosts(self, probe) -> None:
        probe.side_effect = lambda host, port, slave, timeout: {
            "host": host, "tcp_port": port, "slave": slave, "verified": True, "latency_ms": 1,
        }

        network, results = scan_modbus_devices(
            "192.168.2.0/30", 502, "https://example.invalid", excluded_hosts={"192.168.2.1"},
        )

        self.assertEqual(network, "192.168.2.0/30")
        self.assertEqual([item["host"] for item in results], ["192.168.2.2"])

    @patch("device_discovery.automatic_scan_networks")
    @patch("device_discovery.probe_modbus_host")
    def test_automatic_scan_combines_all_active_networks(self, probe, automatic_networks) -> None:
        automatic_networks.return_value = [
            ipaddress.ip_network("192.168.2.0/30"),
            ipaddress.ip_network("10.0.0.0/30"),
        ]
        probe.side_effect = lambda host, port, slave, timeout: {
            "host": host, "tcp_port": port, "slave": slave, "verified": True, "latency_ms": 1,
        }

        networks, results = scan_modbus_networks(None, 502, "https://example.invalid")

        self.assertEqual(networks, ["192.168.2.0/30", "10.0.0.0/30"])
        self.assertEqual(
            [item["host"] for item in results],
            ["10.0.0.1", "10.0.0.2", "192.168.2.1", "192.168.2.2"],
        )

    @patch("dashboard_server.sqlite3.connect")
    def test_dashboard_connection_is_closed_after_failure(self, sqlite_connect) -> None:
        connection = MagicMock()
        sqlite_connect.return_value = connection

        with self.assertRaisesRegex(RuntimeError, "boom"):
            with connect_db() as opened:
                self.assertIs(opened, connection)
                raise RuntimeError("boom")

        connection.close.assert_called_once_with()


class ReadingQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "queries.sqlite3"
        self.db_patch = patch("dashboard_server.DB_PATH", self.db_path)
        self.db_patch.start()
        init_db()
        service = MonitoringService(self.db_path, lambda _name: {}, lambda *_args: {}, lambda *_args: {})
        service.init_schema()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            for index in range(41):
                for room_id, room_name, device_id, device_name in (
                    ("room-001", "Workshop 1", "device-001", "Device 1"),
                    ("room-002", "Workshop 2", "device-002", "Device 2"),
                ):
                    db.execute(
                        """
                        INSERT INTO readings(
                            timestamp, source, host, slave, particles_json, environment_json,
                            alarm_raw, alarms_json, cleanliness_code, cleanliness_label,
                            cleanroom, device, customer_id, cleanroom_id, device_id,
                            alarm_status, alarm_details_json, record_uuid
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            1000 + index * 10, "device", "192.168.2.30", 1,
                            json.dumps({"pm_0_5_um": index}),
                            json.dumps({"temperature": 20 + index / 10, "humidity": 50}),
                            0, "[]", 0, "", room_name, device_name, "customer-001",
                            room_id, device_id, "NORMAL", "[]", f"{room_id}-{index}",
                        ),
                    )

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp.cleanup()

    def test_readings_filter_multiple_scope_identifiers(self) -> None:
        rows = list_readings(
            5000,
            start=1000,
            end=1400,
            customer_id="customer-001",
            cleanroom_ids=["room-002"],
            device_ids=["device-002"],
        )

        self.assertEqual(len(rows), 41)
        self.assertEqual({row["cleanroom_id"] for row in rows}, {"room-002"})
        self.assertEqual({row["device_id"] for row in rows}, {"device-002"})

    def test_trends_sample_each_device_across_the_full_range(self) -> None:
        rows = list_trend_readings(1000, 1400, "customer-001", max_points_per_device=20)
        by_device = {
            device_id: [row for row in rows if row["device_id"] == device_id]
            for device_id in ("device-001", "device-002")
        }

        self.assertEqual(len(rows), 40)
        self.assertEqual({key: len(value) for key, value in by_device.items()}, {
            "device-001": 20,
            "device-002": 20,
        })
        self.assertEqual(by_device["device-001"][0]["timestamp"], 1000)
        self.assertEqual(by_device["device-001"][-1]["timestamp"], 1400)

    def test_trends_keep_all_sparse_rows_below_the_device_limit(self) -> None:
        rows = list_trend_readings(1000, 1400, "customer-001", max_points_per_device=100)

        self.assertEqual(len(rows), 82)
        self.assertEqual(
            {device_id: len([row for row in rows if row["device_id"] == device_id])
             for device_id in ("device-001", "device-002")},
            {"device-001": 41, "device-002": 41},
        )


class BackupSchedulerTests(unittest.TestCase):
    def test_backup_time_and_next_run(self) -> None:
        self.assertEqual(parse_backup_time("02:30"), (2, 30))
        with self.assertRaises(ValueError):
            parse_backup_time("25:00")
        now = datetime(2026, 8, 24, 1, 0)
        self.assertEqual(next_backup_time(now, 2, 0), datetime(2026, 8, 24, 2, 0))
        self.assertEqual(next_backup_time(now.replace(hour=3), 2, 0), datetime(2026, 8, 25, 2, 0))

    def test_existing_daily_backup_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dcp8001_2026-08-24_020000.dump"
            path.touch()
            self.assertTrue(backup_exists_for_date(Path(directory), datetime(2026, 8, 24)))
            self.assertFalse(backup_exists_for_date(Path(directory), datetime(2026, 8, 25)))

    def test_daily_backup_pruning_preserves_recent_and_monthly_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 8, 31, 2, 0)
            old = root / "dcp8001_2026-08-01_020000.dump"
            old_checksum = old.with_suffix(".dump.sha256")
            recent = root / "dcp8001_2026-08-30_020000.dump"
            monthly = root / "monthly" / old.name
            monthly.parent.mkdir()
            for path in (old, old_checksum, recent, monthly):
                path.touch()
            old_time = (now - timedelta(days=30)).timestamp()
            os.utime(old, (old_time, old_time))
            os.utime(old_checksum, (old_time, old_time))

            removed = prune_daily_backups(root, 14, now)

            self.assertEqual(removed, [old])
            self.assertFalse(old.exists())
            self.assertFalse(old_checksum.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(monthly.exists())

    def test_monthly_backup_detection_matches_timestamp_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dcp8001_2026-08-24_020000.dump").touch()

            self.assertTrue(monthly_backup_exists(root, datetime(2026, 8, 31)))
            self.assertFalse(monthly_backup_exists(root, datetime(2026, 9, 1)))

    @unittest.skipIf(os.name == "nt", "POSIX file modes are not enforced on Windows")
    def test_backup_paths_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "daily"
            secure_directory(root)
            backup = root / "example.dump"
            backup.write_bytes(b"backup")
            secure_file(backup)

            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)


class MonitoringServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "test.sqlite3"
        db = sqlite3.connect(self.db_path)
        try:
            db.execute(
                "CREATE TABLE readings (id INTEGER PRIMARY KEY, cleanroom TEXT NOT NULL DEFAULT '', device TEXT NOT NULL DEFAULT '')"
            )
            db.commit()
        finally:
            db.close()
        self.logs: list[tuple[str, str, str]] = []
        self.service = MonitoringService(
            self.db_path,
            lambda _name: {},
            lambda *_args: {},
            lambda level, event, message: self.logs.append((level, event, message)),
            options=MonitorOptions(demo=True),
        )
        self.service.init_schema()
        self.limits = self.service.configuration()[0]["thresholds"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def reading(self, timestamp: float, p05: float, temperature: float = 22, humidity: float = 50) -> dict[str, object]:
        return {
            "timestamp": timestamp,
            "customer_id": "customer-001",
            "cleanroom_id": "room-001",
            "device_id": "device-001",
            "device": "Device 1",
            "particles": {
                "pm_0_3_um": 500000,
                "pm_0_5_um": p05,
                "pm_1_0_um": 40000,
                "pm_2_5_um": 7000,
                "pm_5_0_um": 900,
                "pm_10_0_um": 200,
            },
            "environment": {"temperature": temperature, "humidity": humidity},
        }

    def test_physical_mode_bootstrap_does_not_create_a_phantom_device(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "physical.sqlite3"
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute(
                    "CREATE TABLE readings (id INTEGER PRIMARY KEY, cleanroom TEXT NOT NULL DEFAULT '', device TEXT NOT NULL DEFAULT '')"
                )
            service = MonitoringService(
                db_path,
                lambda _name: {},
                lambda *_args: {},
                lambda *_args: {},
                options=MonitorOptions(demo=False),
            )
            service.init_schema()

            rooms = service.configuration()

            self.assertEqual(len(rooms), 1)
            self.assertEqual(rooms[0]["devices"], [])

    def test_particle_policy_defaults_preserve_only_the_legacy_rule(self) -> None:
        self.assertEqual(self.limits["particle_0_5_max"], 100000)
        self.assertEqual(self.limits["particle_0_5_enabled"], 1)
        for stem in ("0_3", "1_0", "2_5", "5_0", "10_0"):
            self.assertIsNone(self.limits[f"particle_{stem}_max"])
            self.assertEqual(self.limits[f"particle_{stem}_enabled"], 0)

    def test_legacy_particle_limit_payload_still_maps_to_zero_point_five(self) -> None:
        limits = normalise_alarm_thresholds({
            "particle_5_max": 4321,
            "temperature_min": 18,
            "temperature_max": 25,
            "humidity_min": 40,
            "humidity_max": 70,
        })

        self.assertEqual(limits["particle_0_5_max"], 4321)
        self.assertTrue(limits["particle_0_5_enabled"])

    def test_customer_can_configure_every_particle_alarm_channel(self) -> None:
        rooms = self.service.configuration("customer-001")
        for index, stem in enumerate(("0_3", "0_5", "1_0", "2_5", "5_0", "10_0"), 1):
            rooms[0]["thresholds"][f"particle_{stem}_max"] = index * 1000
            rooms[0]["thresholds"][f"particle_{stem}_enabled"] = index % 2 == 1

        self.service.update_customer_settings({"rooms": rooms}, "customer-001")

        saved = self.service.configuration("customer-001")[0]["thresholds"]
        for index, stem in enumerate(("0_3", "0_5", "1_0", "2_5", "5_0", "10_0"), 1):
            self.assertEqual(saved[f"particle_{stem}_max"], index * 1000)
            self.assertEqual(saved[f"particle_{stem}_enabled"], index % 2)

    def test_enabled_particle_rule_requires_a_limit(self) -> None:
        rooms = self.service.configuration("customer-001")
        rooms[0]["thresholds"]["particle_2_5_enabled"] = True
        rooms[0]["thresholds"]["particle_2_5_max"] = None

        with self.assertRaisesRegex(ValueError, "2.5 µm alarm limit is required"):
            self.service.update_customer_settings({"rooms": rooms}, "customer-001")

    def test_non_default_particle_channel_can_alarm(self) -> None:
        limits = dict(self.limits)
        limits.update({
            "particle_5_0_enabled": True,
            "particle_5_0_max": 800,
            "alarm_delay_seconds": 0,
        })

        pending = self.reading(1000, 90000)
        self.service._evaluate_alarms(pending, limits)
        active = self.reading(1001, 90000)
        self.assertTrue(self.service._evaluate_alarms(active, limits))

        self.assertEqual(active["alarm_status"], "ALARM_ACTIVE")
        detail = next(item for item in active["alarm_details"] if item["metric"] == "particle_5_0_um")
        self.assertEqual(detail["state"], "ALARM_ACTIVE")
        events = self.service.alarm_events("customer-001", "room-001")
        self.assertEqual(events[0]["metric"], "particle_5_0_um")

    def test_disabling_an_active_particle_rule_closes_it_immediately(self) -> None:
        limits = dict(self.limits)
        limits.update({
            "particle_5_0_enabled": True,
            "particle_5_0_max": 800,
            "alarm_delay_seconds": 0,
        })
        self.service._evaluate_alarms(self.reading(1000, 90000), limits)
        self.service._evaluate_alarms(self.reading(1001, 90000), limits)

        limits["particle_5_0_enabled"] = False
        after_disable = self.reading(1002, 90000)
        self.assertTrue(self.service._evaluate_alarms(after_disable, limits))

        self.assertEqual(after_disable["alarm_status"], "NORMAL")
        self.assertNotIn("particle_5_0_um", {item["metric"] for item in after_disable["alarm_details"]})
        event = self.service.alarm_events("customer-001", "room-001")[0]
        self.assertEqual(event["ended_at"], 1002)
        with self.service.connect() as db:
            state = db.execute(
                "SELECT 1 FROM alarm_states WHERE device_id=? AND metric=?",
                ("device-001", "particle_5_0_um"),
            ).fetchone()
        self.assertIsNone(state)

    def test_alarm_requires_five_minutes_to_activate_and_clear(self) -> None:
        first = self.reading(1000, 120000)
        self.assertFalse(self.service._evaluate_alarms(first, self.limits))
        self.assertEqual(first["alarm_status"], "PENDING")

        before_delay = self.reading(1299, 120000)
        self.assertFalse(self.service._evaluate_alarms(before_delay, self.limits))
        self.assertEqual(before_delay["alarm_status"], "PENDING")

        active = self.reading(1300, 120000)
        self.assertTrue(self.service._evaluate_alarms(active, self.limits))
        self.assertEqual(active["alarm_status"], "ALARM_ACTIVE")

        clearing = self.reading(1301, 90000)
        self.assertFalse(self.service._evaluate_alarms(clearing, self.limits))
        self.assertEqual(clearing["alarm_status"], "ALARM_ACTIVE")

        cleared = self.reading(1601, 90000)
        self.assertTrue(self.service._evaluate_alarms(cleared, self.limits))
        self.assertEqual(cleared["alarm_status"], "NORMAL")
        events = self.service.alarm_events("customer-001", "room-001")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["started_at"], 1300)
        self.assertEqual(events[0]["ended_at"], 1601)
        self.assertEqual(events[0]["metric"], "particle_0_5_um")

    def test_alarm_log_write_happens_after_alarm_transaction_commits(self) -> None:
        def write_log(_level: str, event: str, _message: str) -> None:
            with closing(sqlite3.connect(self.db_path, timeout=0.1)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS callback_logs(event TEXT NOT NULL)")
                db.execute("INSERT INTO callback_logs(event) VALUES (?)", (event,))

        self.service.add_log = write_log
        self.service._evaluate_alarms(self.reading(1000, 120000), self.limits)
        self.service._evaluate_alarms(self.reading(1300, 120000), self.limits)

        with closing(sqlite3.connect(self.db_path)) as db, db:
            events = [row[0] for row in db.execute("SELECT event FROM callback_logs")]
        self.assertEqual(events, ["alarm_started"])

    def test_customer_update_cannot_change_connection_details(self) -> None:
        rooms = self.service.configuration()
        rooms[0]["devices"][0]["name"] = "Renamed Device"
        rooms[0]["devices"][0]["host"] = "203.0.113.10"
        self.service.update_customer_settings({"rooms": rooms}, "customer-001")
        updated = self.service.configuration()[0]["devices"][0]
        self.assertEqual(updated["name"], "Renamed Device")
        self.assertEqual(updated["host"], "192.168.2.83")

    def test_collector_update_changes_existing_private_connection_only(self) -> None:
        rooms = self.service.configuration("customer-001")
        threshold_before = dict(rooms[0]["thresholds"])
        rooms[0]["devices"][0].update({
            "name": "Wi-Fi Counter",
            "host": "192.168.2.30",
            "tcpPort": 1502,
            "slave": 2,
        })

        self.service.update_collector_connections({"rooms": rooms}, "customer-001")

        updated = self.service.configuration("customer-001")[0]
        self.assertEqual(updated["devices"][0]["name"], "Wi-Fi Counter")
        self.assertEqual(updated["devices"][0]["host"], "192.168.2.30")
        self.assertEqual(updated["devices"][0]["tcpPort"], 1502)
        self.assertEqual(updated["devices"][0]["slave"], 2)
        self.assertEqual(updated["thresholds"], threshold_before)

    def test_collector_update_rejects_non_private_device_address(self) -> None:
        rooms = self.service.configuration("customer-001")
        rooms[0]["devices"][0]["host"] = "203.0.113.10"
        with self.assertRaisesRegex(ValueError, "private IPv4"):
            self.service.update_collector_connections({"rooms": rooms}, "customer-001")

    def test_collector_can_add_and_soft_disable_devices(self) -> None:
        rooms = self.service.configuration("customer-001")
        threshold_before = dict(rooms[0]["thresholds"])
        rooms[0]["devices"].append({
            "id": "", "name": "Wi-Fi Counter 2", "host": "192.168.2.31",
            "tcpPort": 502, "slave": 1,
        })

        self.service.update_collector_connections({"rooms": rooms}, "customer-001")

        updated = self.service.configuration("customer-001")
        self.assertEqual(len(updated[0]["devices"]), 2)
        added = next(item for item in updated[0]["devices"] if item["name"] == "Wi-Fi Counter 2")
        self.assertTrue(str(added["id"]).startswith("device-"))
        self.assertEqual(updated[0]["thresholds"], threshold_before)

        updated[0]["devices"] = [added]
        self.service.update_collector_connections({"rooms": updated}, "customer-001")

        active = self.service.configuration("customer-001")[0]["devices"]
        self.assertEqual([item["id"] for item in active], [added["id"]])
        with self.service.connect() as db:
            disabled = db.execute(
                "SELECT enabled, disabled_at FROM devices WHERE id = 'device-001'"
            ).fetchone()
        self.assertEqual(disabled[0], 0)
        self.assertIsNotNone(disabled[1])

    def test_collector_rejects_duplicate_modbus_endpoint(self) -> None:
        rooms = self.service.configuration("customer-001")
        first = rooms[0]["devices"][0]
        rooms[0]["devices"].append({
            "id": "", "name": "Duplicate", "host": first["host"],
            "tcpPort": first["tcpPort"], "slave": first["slave"],
        })
        with self.assertRaisesRegex(ValueError, "already configured"):
            self.service.update_collector_connections({"rooms": rooms}, "customer-001")

    def test_administrator_can_create_cleanroom_with_default_policy(self) -> None:
        rooms = self.service.create_cleanroom("customer-001", "二楼灌装车间")

        created = next(room for room in rooms if room["name"] == "二楼灌装车间")
        self.assertEqual(created["devices"], [])
        self.assertEqual(created["thresholds"]["particle_0_5_max"], 100000)
        self.assertEqual(created["thresholds"]["particle_0_5_enabled"], 1)
        self.assertEqual(created["thresholds"]["temperature_min"], 18)
        with self.assertRaisesRegex(ValueError, "already in use"):
            self.service.create_cleanroom("customer-001", " 二楼灌装车间 ")

    def test_administrator_can_manually_register_device_in_customer_room(self) -> None:
        rooms = self.service.create_cleanroom("customer-001", "包装车间")
        room = next(item for item in rooms if item["name"] == "包装车间")

        updated = self.service.create_device(
            "customer-001", room["id"], "粒子计数器 02", "192.168.2.31", 502, 1,
        )

        device = next(
            item for item in updated if item["id"] == room["id"]
        )["devices"][0]
        self.assertEqual(device["name"], "粒子计数器 02")
        self.assertEqual(device["host"], "192.168.2.31")
        self.assertTrue(str(device["id"]).startswith("device-"))

    def test_configuration_recovers_last_real_reading_after_restart(self) -> None:
        measured_at = time.time() - 5
        with self.service.connect() as db:
            db.execute("ALTER TABLE readings ADD COLUMN timestamp REAL")
            db.execute("ALTER TABLE readings ADD COLUMN source TEXT")
            db.execute(
                """
                INSERT INTO readings(
                    cleanroom,device,timestamp,source,customer_id,cleanroom_id,device_id
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    "Cleanroom 1", "Device 1", measured_at, "device",
                    "customer-001", "room-001", "device-001",
                ),
            )

        restored = self.service.configuration("customer-001")[0]["devices"][0]

        self.assertAlmostEqual(float(restored["last_seen_at"]), measured_at, places=3)
        self.assertTrue(restored["online"])

    def test_manual_device_registration_enforces_tenant_and_endpoint_boundaries(self) -> None:
        room_id = self.service.configuration("customer-001")[0]["id"]
        with self.assertRaisesRegex(ValueError, "Cleanroom not found"):
            self.service.create_device(
                "customer-002", room_id, "Other tenant device", "192.168.2.31", 502, 1,
            )
        with self.assertRaisesRegex(ValueError, "already configured"):
            self.service.create_device(
                "customer-001", room_id, "Duplicate endpoint", "192.168.2.83", 502, 1,
            )
        with self.assertRaisesRegex(ValueError, "private IPv4"):
            self.service.create_device(
                "customer-001", room_id, "Public endpoint", "203.0.113.10", 502, 1,
            )

    @patch("monitoring_service.Dcp8001TcpClient")
    def test_collector_can_test_unsaved_device_connection(self, client_type) -> None:
        reading = MagicMock()
        reading.timestamp = 1000.0
        reading.particles = {"pm_0_5_um": 123}
        reading.environment = {"temperature": 22.4, "humidity": 48.2}
        client_type.return_value.read_realtime.return_value = reading

        result = self.service.test_device_connection({
            "host": "192.168.2.31", "tcpPort": 502, "slave": 1,
        })

        self.assertEqual(result["source"], "device")
        self.assertEqual(result["sample"]["temperature"], 22.4)
        client_type.return_value.close.assert_called_once_with()

    @patch("monitoring_service.Dcp8001TcpClient")
    def test_collector_test_reuses_configured_device_connection(self, client_type) -> None:
        reading = MagicMock()
        reading.timestamp = 1000.0
        reading.particles = {"pm_0_5_um": 123}
        reading.environment = {"temperature": 22.4, "humidity": 48.2}
        client_type.return_value.read_realtime.return_value = reading
        device = self.service.configuration("customer-001")[0]["devices"][0]

        first = self.service._client_for_device(device)
        result = self.service.test_device_connection({
            "host": device["host"], "tcpPort": device["tcpPort"], "slave": device["slave"],
        })

        self.assertEqual(result["source"], "device")
        self.assertEqual(client_type.call_count, 1)
        self.assertIs(self.service._client_for_device(device), first)
        client_type.return_value.close.assert_not_called()

    def test_monitor_uses_bounded_parallel_device_workers(self) -> None:
        rooms = self.service.configuration("customer-001")
        rooms[0]["devices"].append({
            "id": "", "name": "Wi-Fi Counter 2", "host": "192.168.2.31",
            "tcpPort": 502, "slave": 1,
        })
        self.service.update_collector_connections({"rooms": rooms}, "customer-001")
        self.service.options.poll_workers = 2
        self.service.options.poll_seconds = 60
        both_running = threading.Event()
        release = threading.Event()
        state_lock = threading.Lock()
        active = 0
        maximum = 0

        def fake_poll(_room, device) -> None:
            nonlocal active, maximum
            with state_lock:
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    both_running.set()
            release.wait(2)
            with self.service._lock:
                self.service._next_poll_at[str(device["id"])] = time.monotonic() + 60
            with state_lock:
                active -= 1

        self.service._poll_device = fake_poll
        self.service.start()
        try:
            self.assertTrue(both_running.wait(2), "two devices were not polled concurrently")
            self.assertEqual(maximum, 2)
        finally:
            release.set()
            self.service.stop()

    @patch("monitoring_service.Dcp8001TcpClient")
    def test_diagnostics_summarize_failures_and_record_recovery(self, client_type):
        room = self.service.configuration()[0]
        device = room["devices"][0]
        self.service.options.demo = False
        client = client_type.return_value
        client.diagnostic_stage = "initial_settle"
        client.request_sent = False
        client.read_realtime.side_effect = ConnectionError("Socket closed while waiting for initial Modbus TCP data")
        self.service._poll_device(room, device)
        self.service._poll_device(room, device)
        failures = [message for _level, event, message in self.logs if event == "device_offline"]
        self.assertEqual(len(failures), 1)
        self.assertIn("stage=initial_settle request_sent=False", failures[0])
        self.assertIn("endpoint=", failures[0])
        self.assertEqual(self.service._device_failures[device["id"]], 2)
        client.read_realtime.side_effect = None
        client.read_realtime.return_value = self.reading(time.time(), 500)
        self.service._poll_device(room, device)
        recovered = [message for _level, event, message in self.logs if event == "device_recovered"]
        self.assertEqual(len(recovered), 1)
        self.assertIn("recovered_after_failures=2", recovered[0])
        self.assertTrue(self.service.latest[device["id"]]["online"])
        self.service._device_failures[device["id"]] = 1
        self.service.add_log = MagicMock(side_effect=OSError("log disk unavailable"))
        self.service._poll_device(room, device)
        self.assertTrue(self.service.latest[device["id"]]["online"])

    @patch("monitoring_service.Dcp8001TcpClient")
    def test_monitor_reuses_device_connection_until_endpoint_changes(self, client_type) -> None:
        first_client = MagicMock()
        second_client = MagicMock()
        client_type.side_effect = [first_client, second_client]
        device = self.service.configuration()[0]["devices"][0]

        self.assertIs(self.service._client_for_device(device), first_client)
        self.assertIs(self.service._client_for_device(device), first_client)
        self.assertEqual(client_type.call_count, 1)

        changed = {**device, "host": "192.168.2.30"}
        self.assertIs(self.service._client_for_device(changed), second_client)
        first_client.close.assert_called_once_with()
        self.assertEqual(client_type.call_count, 2)

        self.service._close_all_device_clients()
        second_client.close.assert_called_once_with()

    def test_configuration_is_isolated_by_customer(self) -> None:
        with self.service.connect() as db:
            db.execute(
                "INSERT INTO cleanrooms(id, customer_id, name, sort_order) VALUES (?, ?, ?, ?)",
                ("other-room", "customer-002", "Other Customer Room", 1),
            )
            db.execute(
                """
                INSERT INTO thresholds(cleanroom_id, profile_name, particle_0_5_max, particle_5_max,
                    temperature_min, temperature_max, humidity_min, humidity_max, alarm_delay_seconds)
                VALUES (?, 'Custom', 1000, 1000, 18, 26, 40, 60, 300)
                """,
                ("other-room",),
            )
        first = self.service.configuration("customer-001")
        second = self.service.configuration("customer-002")
        self.assertTrue(all(room["customer_id"] == "customer-001" for room in first))
        self.assertEqual([room["id"] for room in second], ["other-room"])

    def test_cloud_configuration_adds_updates_and_disables_devices(self) -> None:
        payload = {
            "customer_id": "addvalue", "site_id": "addvalue-site-001", "revision": 2,
            "rooms": [{
                "id": "addvalue-room-001", "name": "Production Room",
                "devices": [{
                    "id": "addvalue-device-002", "name": "Particle Counter 2",
                    "host": "192.168.10.88", "tcpPort": 1502, "slave": 2,
                }],
                "thresholds": {
                    "profile_name": "Class 100K", "particle_0_5_max": 100000,
                    "temperature_min": 18, "temperature_max": 25,
                    "humidity_min": 40, "humidity_max": 70,
                    "alarm_delay_seconds": 300,
                },
            }],
        }
        self.service.apply_edge_configuration(payload)
        rooms = self.service.configuration("addvalue")
        self.assertEqual([room["id"] for room in rooms], ["addvalue-room-001"])
        device = rooms[0]["devices"][0]
        self.assertEqual(device["id"], "addvalue-device-002")
        self.assertEqual(device["host"], "192.168.10.88")
        self.assertEqual(device["tcpPort"], 1502)
        self.assertEqual(device["site_id"], "addvalue-site-001")
        thresholds = rooms[0]["thresholds"]
        self.assertEqual(thresholds["particle_0_5_enabled"], 1)
        self.assertEqual(thresholds["particle_10_0_enabled"], 0)

        payload["rooms"][0]["thresholds"].update({
            "particle_10_0_max": 500,
            "particle_10_0_enabled": True,
        })
        self.service.apply_edge_configuration(payload)
        legacy_payload = json.loads(json.dumps(payload))
        legacy_payload["rooms"][0]["thresholds"].pop("particle_10_0_max")
        legacy_payload["rooms"][0]["thresholds"].pop("particle_10_0_enabled")
        self.service.apply_edge_configuration(legacy_payload)
        preserved = self.service.configuration("addvalue")[0]["thresholds"]
        self.assertEqual(preserved["particle_10_0_max"], 500)
        self.assertEqual(preserved["particle_10_0_enabled"], 1)
        with self.service.connect() as db:
            self.assertEqual(db.execute("SELECT enabled FROM devices WHERE id='device-001'").fetchone()[0], 0)


class ExcelExportTests(unittest.TestCase):
    def test_workbook_has_summary_and_cleanroom_sheets(self) -> None:
        row = {
            "timestamp": 1000.0,
            "device": "Device 1",
            "particles": {
                "pm_0_3_um": 1, "pm_0_5_um": 2, "pm_1_0_um": 3,
                "pm_2_5_um": 4, "pm_5_0_um": 5, "pm_10_0_um": 6,
            },
            "environment": {"temperature": 22.5, "humidity": 50.0},
            "alarm_status": "NORMAL",
            "alarm_details": [],
        }
        body = workbook_xlsx({"Cleanroom 1": [row], "Cleanroom 2": []})
        archive_path = Path(tempfile.gettempdir()) / "dcp8001-test.xlsx"
        archive_path.write_bytes(body)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                workbook = archive.read("xl/workbook.xml").decode("utf-8")
                self.assertIn('name="Summary"', workbook)
                self.assertIn('name="Cleanroom 1"', workbook)
                self.assertIn('name="Cleanroom 2"', workbook)
                summary = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
                self.assertIn("Alarm Count", summary)
                room = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
                self.assertIn("particles/ft³", room)
                self.assertIn("Alarm Status", room)
        finally:
            archive_path.unlink(missing_ok=True)

    def test_workbook_can_render_chinese_without_translating_business_names(self) -> None:
        row = {
            "timestamp": 1000.0,
            "device": "温度",
            "particles": {"pm_0_5_um": 12},
            "environment": {"temperature": 22.5, "humidity": 50.0},
            "alarm_status": "ALARM_ACTIVE",
            "alarm_details": [
                {"metric": "humidity", "state": "ALARM_ACTIVE", "limit": "> 60 %RH"},
            ],
        }
        body = workbook_xlsx({"英文车间": [row]}, locale="zh-CN")
        archive_path = Path(tempfile.gettempdir()) / "dcp8001-test-zh-CN.xlsx"
        archive_path.write_bytes(body)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                workbook = archive.read("xl/workbook.xml").decode("utf-8")
                self.assertIn('name="汇总"', workbook)
                self.assertIn('name="英文车间"', workbook)
                summary = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
                self.assertIn("报警次数", summary)
                self.assertIn("温度", summary)
                room = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
                self.assertIn("报警状态", room)
                self.assertIn("报警中", room)
                self.assertIn("湿度: &gt; 60 %RH", room)
                self.assertIn("温度", room)
        finally:
            archive_path.unlink(missing_ok=True)

    def test_report_locale_helpers_only_translate_system_values(self) -> None:
        self.assertEqual(normalize_report_locale("zh-Hans"), "zh-CN")
        self.assertEqual(normalize_report_locale("fr-FR"), "en-US")
        self.assertEqual(report_status("NORMAL", "zh-CN"), "正常")
        self.assertEqual(report_status("NORMAL", "en-US"), "Normal")
        self.assertEqual(
            report_alarm_details(
                [{"metric": "humidity", "state": "ALARM_ACTIVE", "limit": "> 60 %RH"}],
                "en-US",
            ),
            "Humidity: > 60 %RH",
        )


class AuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "auth.sqlite3"
        self.auth = AuthService(self.db_path)
        self.credentials = self.auth.init_schema()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_login_session_and_logout(self) -> None:
        self.assertIsNotNone(self.credentials)
        username, password = self.credentials
        with self.assertRaises(AuthenticationError):
            self.auth.login(username, "wrong-password")
        token, user = self.auth.login(username, password)
        self.assertEqual(user["customer_id"], "customer-001")
        self.assertEqual(self.auth.user_for_token(token)["username"], username)
        self.auth.logout(token)
        self.assertIsNone(self.auth.user_for_token(token))


class CloudSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "sync.sqlite3"
        db = sqlite3.connect(self.db_path)
        try:
            db.execute(
                """
                CREATE TABLE readings (
                    id INTEGER PRIMARY KEY, record_uuid TEXT UNIQUE, customer_id TEXT,
                    site_id TEXT, cleanroom_id TEXT, cleanroom TEXT, device_id TEXT,
                    device TEXT, timestamp REAL, source TEXT, particles_json TEXT,
                    environment_json TEXT, alarm_status TEXT, alarm_details_json TEXT,
                    cleanliness_code INTEGER, cleanliness_label TEXT, synced_at REAL,
                    sync_attempts INTEGER DEFAULT 0, sync_error TEXT,
                    sync_quarantined_at REAL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE alarm_events (
                    id TEXT PRIMARY KEY, customer_id TEXT, site_id TEXT DEFAULT 'site-001',
                    cleanroom_id TEXT, device_id TEXT, source TEXT DEFAULT 'device',
                    metric TEXT, started_at REAL, ended_at REAL, trigger_value REAL,
                    peak_value REAL, limit_description TEXT, synced_at REAL,
                    sync_attempts INTEGER DEFAULT 0, sync_error TEXT,
                    sync_quarantined_at REAL
                )
                """
            )
            db.execute(
                """
                INSERT INTO readings(record_uuid, customer_id, site_id, cleanroom_id, cleanroom,
                    device_id, device, timestamp, source, particles_json, environment_json,
                    alarm_status, alarm_details_json, cleanliness_code, cleanliness_label)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "11111111111141118111111111111111", "customer-001", "site-001",
                    "room-001", "Cleanroom 1", "device-001", "Device 1", 1000.0, "device",
                    json.dumps({"pm_5_0_um": 5}), json.dumps({"temperature": 22, "humidity": 50}),
                    "NORMAL", "[]", 7, "CLASS 7",
                ),
            )
            db.execute(
                """
                INSERT INTO readings(record_uuid, customer_id, site_id, cleanroom_id, cleanroom,
                    device_id, device, timestamp, source, particles_json, environment_json,
                    alarm_status, alarm_details_json, cleanliness_code, cleanliness_label)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "66666666666646668666666666666666", "customer-001", "old-site",
                    "room-001", "Cleanroom 1", "old-device", "Old Site Device", 1001.5,
                    "device", json.dumps({"pm_5_0_um": 8}),
                    json.dumps({"temperature": 22, "humidity": 51}), "NORMAL", "[]", 7,
                    "CLASS 7",
                ),
            )
            db.commit()
        finally:
            db.close()
        self.received: list[dict[str, object]] = []
        self.received_instances: list[str] = []
        received = self.received
        received_instances = self.received_instances

        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler_self) -> None:
                received_instances.append(handler_self.headers.get("X-DCP-Collector-Instance", ""))
                response = json.dumps({
                    "ok": True,
                    "data": {
                        "customer_id": "customer-001", "site_id": "site-001",
                        "site_name": "Main collector", "revision": 2,
                        "rooms": [],
                    },
                }).encode()
                handler_self.send_response(200)
                handler_self.send_header("Content-Type", "application/json")
                handler_self.send_header("Content-Length", str(len(response)))
                handler_self.end_headers()
                handler_self.wfile.write(response)

            def do_POST(handler_self) -> None:
                body = handler_self.rfile.read(int(handler_self.headers["Content-Length"]))
                payload = json.loads(body)
                received.append(payload)
                received_instances.append(handler_self.headers.get("X-DCP-Collector-Instance", ""))
                if handler_self.path.endswith("/edge/heartbeat"):
                    response = json.dumps({
                        "ok": True,
                        "data": {"desired_config_version": 2, "heartbeat_at": time.time()},
                    }).encode()
                    handler_self.send_response(200)
                    handler_self.send_header("Content-Type", "application/json")
                    handler_self.send_header("Content-Length", str(len(response)))
                    handler_self.end_headers()
                    handler_self.wfile.write(response)
                    return
                if handler_self.path.endswith(("/edge/cleanrooms", "/edge/devices")):
                    response = json.dumps({
                        "ok": True,
                        "data": {
                            "customer_id": "customer-001", "site_id": "site-001",
                            "revision": 3, "rooms": [{
                                "id": "room-002", "name": "Workshop 2", "devices": [],
                                "thresholds": {},
                            }],
                        },
                    }).encode()
                    handler_self.send_response(201)
                    handler_self.send_header("Content-Type", "application/json")
                    handler_self.send_header("Content-Length", str(len(response)))
                    handler_self.end_headers()
                    handler_self.wfile.write(response)
                    return
                if (
                    handler_self.path.endswith("/readings/batch")
                    and any(item.get("device_id") == "temporary-device" for item in payload["readings"])
                ):
                    response = json.dumps({"detail": "temporarily unavailable"}).encode()
                    handler_self.send_response(503)
                    handler_self.send_header("Content-Type", "application/json")
                    handler_self.send_header("Content-Length", str(len(response)))
                    handler_self.end_headers()
                    handler_self.wfile.write(response)
                    return
                if (
                    handler_self.path.endswith("/readings/batch")
                    and any(item.get("device_id") == "bad-device" for item in payload["readings"])
                ):
                    response = json.dumps({"detail": "invalid reading"}).encode()
                    handler_self.send_response(422)
                    handler_self.send_header("Content-Type", "application/json")
                    handler_self.send_header("Content-Length", str(len(response)))
                    handler_self.end_headers()
                    handler_self.wfile.write(response)
                    return
                if handler_self.path.endswith("/latest/batch"):
                    accepted = len(payload["readings"])
                elif handler_self.path.endswith("/alarms/batch"):
                    accepted = [item["event_uuid"] for item in payload["events"]]
                else:
                    # PostgreSQL/Pydantic returns UUIDs with hyphens even when the
                    # edge database stores the same UUID as 32 compact hex digits.
                    accepted = [str(UUID(item["record_uuid"])) for item in payload["readings"]]
                response = json.dumps({"ok": True, "accepted": accepted}).encode()
                handler_self.send_response(200)
                handler_self.send_header("Content-Type", "application/json")
                handler_self.send_header("Content-Length", str(len(response)))
                handler_self.end_headers()
                handler_self.wfile.write(response)

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_successful_batch_is_marked_synced_and_not_resent(self) -> None:
        service = CloudSyncService(
            self.db_path, f"http://127.0.0.1:{self.server.server_port}", "test-token",
            "site-001", lambda *_args: None,
        )
        self.assertEqual(service.pending_count(), 1)
        self.assertEqual(service.sync_once(), 1)
        self.assertEqual(service.pending_count(), 0)
        self.assertEqual(service.sync_once(), 0)
        self.assertEqual(len(self.received), 1)

    def test_automatic_discovery_reports_all_active_networks(self) -> None:
        service = CloudSyncService(
            self.db_path, "https://dashboard.example.com", "test-token",
            "site-001", lambda *_args: None,
            discovery_scanner=lambda *_args: (
                ["192.168.2.0/24", "192.168.3.0/24"],
                [{
                    "host": "192.168.2.30", "tcp_port": 502, "slave": 1,
                    "verified": True, "modbus_responded": True,
                    "protocol_compatible": True, "identity_verified": False,
                    "firmware_raw": 100, "particle_unit_code": 1,
                    "particle_unit_label": "PCS/28.3L", "unit_supported": True,
                    "latency_ms": 3,
                }],
            ),
        )
        requests = []

        def fake_request(request):
            requests.append(request)
            if request.get_method() == "GET":
                return {"ok": True, "data": {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "site_id": "site-001", "requested_cidr": None,
                    "tcp_port": 502, "source": "automatic",
                }}
            return {"ok": True, "accepted": 1}

        service._request_json = fake_request
        self.assertEqual(service.pull_discovery_job_once(), 1)
        completion = json.loads(requests[1].data.decode("utf-8"))
        self.assertEqual(
            completion["scanned_cidrs"],
            ["192.168.2.0/24", "192.168.3.0/24"],
        )
        self.assertEqual(completion["results"][0]["host"], "192.168.2.30")

    @patch("cloud_sync.scan_modbus_networks")
    def test_automatic_discovery_skips_already_assigned_hosts(self, scanner) -> None:
        scanner.return_value = (["192.168.2.0/24"], [])
        service = CloudSyncService(
            self.db_path, "https://dashboard.example.com", "test-token",
            "site-001", lambda *_args: None,
        )
        service._apply_configuration_payload(
            {
                "site_name": "Windows 01",
                "rooms": [
                    {"devices": [
                        {"host": "192.168.2.30", "enabled": True},
                        {"host": "192.168.2.31", "enabled": False},
                    ]},
                ],
            },
            service._last_config_revision or 0,
        )

        service.discovery_scanner(None, 502, "https://dashboard.example.com")

        scanner.assert_called_once_with(
            None, 502, "https://dashboard.example.com",
            excluded_hosts={"192.168.2.30"},
        )

    def test_particle_unit_and_protocol_profile_are_uploaded_when_present(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("ALTER TABLE readings ADD COLUMN particle_unit_code INTEGER")
            db.execute("ALTER TABLE readings ADD COLUMN particle_unit_label TEXT")
            db.execute("ALTER TABLE readings ADD COLUMN protocol_profile TEXT")
            db.execute(
                """UPDATE readings
                   SET particle_unit_code = 1, particle_unit_label = 'PCS/28.3L',
                       protocol_profile = 'dcp8001-protocol-2025-06-04'
                   WHERE site_id = 'site-001'"""
            )

        service = CloudSyncService(
            self.db_path, f"http://127.0.0.1:{self.server.server_port}", "test-token",
            "site-001", lambda *_args: None,
        )
        self.assertEqual(service.sync_once(), 1)
        uploaded = self.received[0]["readings"][0]
        self.assertEqual(uploaded["particle_unit_code"], 1)
        self.assertEqual(uploaded["particle_unit_label"], "PCS/28.3L")
        self.assertEqual(uploaded["protocol_profile"], "dcp8001-protocol-2025-06-04")

    def test_edge_configuration_is_applied_once_per_revision(self) -> None:
        applied: list[dict[str, object]] = []
        service = CloudSyncService(
            self.db_path, f"http://127.0.0.1:{self.server.server_port}", "test-token",
            "site-001", lambda *_args: None, config_applier=applied.append,
        )
        self.assertEqual(service.pull_config_once(), 2)
        self.assertEqual(service.pull_config_once(), 0)
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["site_id"], "site-001")

    def test_heartbeat_reports_runtime_health_and_configuration_receipt(self) -> None:
        service = CloudSyncService(
            self.db_path, f"http://127.0.0.1:{self.server.server_port}", "test-token",
            "site-001", lambda *_args: None, config_applier=lambda _payload: None,
            status_provider=lambda: {
                "monitor_running": True,
                "device_total": 4,
                "device_online": 3,
                "last_reading_at": 1001.0,
                "storage": {
                    "state": "ok", "database_bytes": 4096, "disk_free_bytes": 1024**3,
                },
            },
        )

        self.assertEqual(service.pull_config_once(), 2)
        self.assertEqual(service.heartbeat_once(), 2)

        heartbeat = self.received[-1]
        self.assertEqual(heartbeat["site_id"], "site-001")
        self.assertEqual(heartbeat["device_total"], 4)
        self.assertEqual(heartbeat["device_online"], 3)
        self.assertEqual(heartbeat["storage_state"], "ok")
        self.assertEqual(heartbeat["applied_config_version"], 2)
        self.assertEqual(heartbeat["config_apply_status"], "applied")
        self.assertTrue(all(value == service.instance_id for value in self.received_instances))
        self.assertEqual(service.site_name, "Main collector")
        self.assertIsNotNone(service.connection_status()["last_heartbeat_at"])

    def test_collector_instance_and_applied_revision_survive_restart(self) -> None:
        first = CloudSyncService(
            self.db_path, f"http://127.0.0.1:{self.server.server_port}", "test-token",
            "site-001", lambda *_args: None, config_applier=lambda _payload: None,
        )
        self.assertEqual(first.pull_config_once(), 2)

        restarted = CloudSyncService(
            self.db_path, f"http://127.0.0.1:{self.server.server_port}", "test-token",
            "site-001", lambda *_args: None, config_applier=lambda _payload: None,
        )

        self.assertEqual(restarted.instance_id, first.instance_id)
        self.assertEqual(restarted.connection_status()["applied_config_version"], 2)
        self.assertEqual(restarted.pull_config_once(), 0)

    def test_failed_configuration_is_reported_without_advancing_applied_revision(self) -> None:
        def reject(_payload: dict[str, object]) -> None:
            raise ValueError("invalid local endpoint")

        service = CloudSyncService(
            self.db_path, f"http://127.0.0.1:{self.server.server_port}", "test-token",
            "site-001", lambda *_args: None, config_applier=reject,
        )

        with self.assertRaisesRegex(ValueError, "invalid local endpoint"):
            service.pull_config_once()
        service.heartbeat_once()

        heartbeat = self.received[-1]
        self.assertIsNone(heartbeat["applied_config_version"])
        self.assertEqual(heartbeat["config_apply_status"], "failed")
        self.assertEqual(heartbeat["config_apply_error"], "invalid local endpoint")

    def test_manual_topology_change_is_created_in_cloud_and_applied_locally(self) -> None:
        applied: list[dict[str, object]] = []
        service = CloudSyncService(
            self.db_path, f"http://127.0.0.1:{self.server.server_port}", "test-token",
            "site-001", lambda *_args: None, config_applier=applied.append,
        )

        rooms = service.create_cleanroom_once("Workshop 2")

        self.assertEqual(rooms[0]["name"], "Workshop 2")
        self.assertEqual(self.received[-1], {"name": "Workshop 2"})
        self.assertEqual(applied[-1]["revision"], 3)

    def test_alarm_event_is_uploaded_and_acknowledged(self) -> None:
        db = sqlite3.connect(self.db_path)
        try:
            db.execute(
                """
                INSERT INTO alarm_events(id,customer_id,cleanroom_id,device_id,metric,
                    started_at,trigger_value,peak_value,limit_description)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    "22222222-2222-4222-8222-222222222222", "customer-001", "room-001",
                    "device-001", "temperature", 1000.0, 26.0, 27.0, "outside 18-25 C",
                ),
            )
            db.commit()
        finally:
            db.close()
        service = CloudSyncService(
            self.db_path, f"http://127.0.0.1:{self.server.server_port}", "test-token",
            "site-001", lambda *_args: None,
        )
        self.assertEqual(service.sync_alarms_once(), 1)
        db = sqlite3.connect(self.db_path)
        try:
            self.assertIsNotNone(db.execute("SELECT synced_at FROM alarm_events").fetchone()[0])
        finally:
            db.close()
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0]["site_id"], "site-001")
        self.assertEqual(self.received[0]["events"][0]["customer_id"], "customer-001")

    def test_latest_channel_uploads_without_creating_history_rows(self) -> None:
        latest = {
            "customer_id": "customer-001", "site_id": "site-001", "cleanroom_id": "room-001",
            "cleanroom": "Cleanroom 1", "device_id": "device-001", "device": "Device 1",
            "timestamp": 1001.0, "source": "device", "particles": {"pm_5_0_um": 7},
            "environment": {"temperature": 22, "humidity": 50}, "alarm_status": "NORMAL",
            "alarm_details": [], "cleanliness_code": 7, "cleanliness_label": "CLASS 7",
            "online": True,
        }
        service = CloudSyncService(
            self.db_path, f"http://127.0.0.1:{self.server.server_port}", "test-token",
            "site-001", lambda *_args: None, latest_provider=lambda: [latest],
        )
        self.assertEqual(service.sync_latest_once(), 1)
        self.assertEqual(self.received[0]["readings"][0]["measured_at"], 1001.0)
        self.assertEqual(service.pending_count(), 1)

    def test_demo_readings_never_enter_cloud_channels(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                """
                INSERT INTO readings(record_uuid, customer_id, site_id, cleanroom_id, cleanroom,
                    device_id, device, timestamp, source, particles_json, environment_json,
                    alarm_status, alarm_details_json, cleanliness_code, cleanliness_label)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "33333333333343338333333333333333", "customer-001", "site-001",
                    "room-001", "Cleanroom 1", "device-demo", "Demo Device", 1002.0,
                    "demo", json.dumps({"pm_5_0_um": 9}),
                    json.dumps({"temperature": 23, "humidity": 55}), "NORMAL", "[]", 7,
                    "CLASS 7",
                ),
            )
        latest = {
            "customer_id": "customer-001", "site_id": "site-001",
            "cleanroom_id": "room-001", "cleanroom": "Cleanroom 1",
            "device_id": "device-demo", "device": "Demo Device", "timestamp": 1002.0,
            "source": "demo", "particles": {"pm_5_0_um": 9},
            "environment": {"temperature": 23, "humidity": 55},
            "alarm_status": "NORMAL", "alarm_details": [], "cleanliness_code": 7,
            "cleanliness_label": "CLASS 7", "online": True,
        }
        service = CloudSyncService(
            self.db_path, f"http://127.0.0.1:{self.server.server_port}", "test-token",
            "site-001", lambda *_args: None, latest_provider=lambda: [latest],
        )
        self.assertEqual(service.pending_count(), 1)
        self.assertEqual(service.unassigned_count(), 1)
        self.assertEqual(service.sync_latest_once(), 0)
        self.assertEqual(service.sync_once(), 1)
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0]["readings"][0]["source"], "device")
        self.assertEqual(self.received[0]["readings"][0]["site_id"], "site-001")
        with closing(sqlite3.connect(self.db_path)) as db, db:
            self.assertIsNone(
                db.execute("SELECT synced_at FROM readings WHERE site_id='old-site'").fetchone()[0]
            )

    def test_invalid_reading_isolated_then_quarantined_without_blocking_good_data(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                """
                INSERT INTO readings(record_uuid, customer_id, site_id, cleanroom_id, cleanroom,
                    device_id, device, timestamp, source, particles_json, environment_json,
                    alarm_status, alarm_details_json, cleanliness_code, cleanliness_label)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "44444444444444448444444444444444", "customer-001", "site-001",
                    "room-001", "Cleanroom 1", "bad-device", "Invalid Device", 999.0,
                    "device", json.dumps({"pm_5_0_um": 9}),
                    json.dumps({"temperature": 23, "humidity": 55}), "NORMAL", "[]", 7,
                    "CLASS 7",
                ),
            )
        logs: list[tuple[str, str, str]] = []
        service = CloudSyncService(
            self.db_path, f"http://127.0.0.1:{self.server.server_port}", "test-token",
            "site-001", lambda *values: logs.append(values), max_item_attempts=3,
        )

        self.assertEqual(service.sync_once(), 1)
        self.assertEqual(service.pending_count(), 1)
        self.assertEqual(service.sync_once(), 0)
        self.assertEqual(service.pending_count(), 1)
        self.assertEqual(service.sync_once(), 0)
        self.assertEqual(service.pending_count(), 0)
        self.assertEqual(service.quarantined_count(), 1)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            attempts, quarantined_at = db.execute(
                "SELECT sync_attempts, sync_quarantined_at FROM readings WHERE device_id='bad-device'"
            ).fetchone()
        self.assertEqual(attempts, 3)
        self.assertIsNotNone(quarantined_at)
        self.assertTrue(any(event == "cloud_item_quarantined" for _level, event, _message in logs))

        service._thread = threading.current_thread()
        status = service.connection_status()
        self.assertEqual(status["state"], "attention")
        self.assertTrue(status["connected"])
        self.assertEqual(status["quarantined_uploads"], 1)
        self.assertEqual(status["unassigned_uploads"], 1)

    def test_temporary_cloud_failure_does_not_quarantine_or_consume_item_retries(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                """
                INSERT INTO readings(record_uuid, customer_id, site_id, cleanroom_id, cleanroom,
                    device_id, device, timestamp, source, particles_json, environment_json,
                    alarm_status, alarm_details_json, cleanliness_code, cleanliness_label)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "55555555555545558555555555555555", "customer-001", "site-001",
                    "room-001", "Cleanroom 1", "temporary-device", "Temporary Device", 999.0,
                    "device", json.dumps({"pm_5_0_um": 9}),
                    json.dumps({"temperature": 23, "humidity": 55}), "NORMAL", "[]", 7,
                    "CLASS 7",
                ),
            )
        service = CloudSyncService(
            self.db_path, f"http://127.0.0.1:{self.server.server_port}", "test-token",
            "site-001", lambda *_args: None, max_item_attempts=1,
        )

        with self.assertRaises(urllib.error.HTTPError):
            service.sync_once()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            rows = db.execute(
                """SELECT sync_attempts, sync_quarantined_at, sync_error FROM readings
                   WHERE site_id='site-001'"""
            ).fetchall()
        self.assertTrue(all(attempts == 0 for attempts, _quarantined, _error in rows))
        self.assertTrue(all(quarantined is None for _attempts, quarantined, _error in rows))
        self.assertTrue(all(error for _attempts, _quarantined, error in rows))
        self.assertEqual(service.pending_count(), 2)
        self.assertEqual(service.quarantined_count(), 0)


if __name__ == "__main__":
    unittest.main()
