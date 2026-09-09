from __future__ import annotations

import ipaddress
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from alarm_config import (
    DEFAULT_THRESHOLDS,
    PARTICLE_ALARM_CHANNELS,
    normalise_alarm_thresholds,
)
from auth_service import DEFAULT_CUSTOMER_ID
from dcp8001_collector import Dcp8001TcpClient
from collector_diagnostics import diagnostic_scope

PRIVATE_DEVICE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_enabled(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


@dataclass
class MonitorOptions:
    poll_seconds: float = _env_float("DCP_POLL_SECONDS", 10.0)
    record_seconds: float = _env_float("DCP_RECORD_SECONDS", 120.0)
    device_timeout: float = _env_float("DCP_DEVICE_TIMEOUT", 5.0)
    poll_workers: int = max(1, min(_env_int("DCP_POLL_WORKERS", 8), 32))
    offline_backoff_max: float = max(10.0, _env_float("DCP_OFFLINE_BACKOFF_MAX", 10.0))
    demo: bool = _env_enabled("DCP_DEMO_MODE")


MAX_ACTIVE_DEVICES = max(1, min(_env_int("DCP_MAX_ACTIVE_DEVICES", 100), 1000))
MAX_CLEANROOMS = max(1, min(_env_int("DCP_MAX_CLEANROOMS", 200), 1000))


class MonitoringService:
    """Continuously reads devices, evaluates alarms and periodically persists readings."""

    def __init__(
        self,
        db_path: Path,
        demo_factory: Callable[[str], dict[str, object]],
        record_reading: Callable[..., dict[str, object]],
        add_log: Callable[[str, str, str], None],
        options: MonitorOptions | None = None,
    ) -> None:
        self.db_path = db_path
        self.demo_factory = demo_factory
        self.record_reading = record_reading
        self.add_log = add_log
        self.options = options or MonitorOptions()
        self.latest: dict[str, dict[str, object]] = {}
        self.last_recorded: dict[str, float] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client_lock = threading.RLock()
        self._device_clients: dict[
            str, tuple[tuple[str, int, int], Dcp8001TcpClient]
        ] = {}
        self._device_io_locks: dict[str, threading.RLock] = {}
        self._device_failures: dict[str, int] = {}
        self._failure_log_state: dict[str, tuple[str, float, int]] = {}
        self._next_poll_at: dict[str, float] = {}

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def init_schema(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS cleanrooms (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL DEFAULT 'customer-001',
                    name TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    cleanroom_id TEXT NOT NULL REFERENCES cleanrooms(id),
                    site_id TEXT,
                    name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    tcp_port INTEGER NOT NULL DEFAULT 502,
                    slave INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    disabled_at REAL,
                    updated_at REAL
                );
                CREATE TABLE IF NOT EXISTS thresholds (
                    cleanroom_id TEXT PRIMARY KEY REFERENCES cleanrooms(id),
                    profile_name TEXT NOT NULL DEFAULT 'Custom',
                    particle_0_3_max REAL,
                    particle_0_3_enabled INTEGER NOT NULL DEFAULT 0,
                    particle_0_5_max REAL NOT NULL DEFAULT 100000,
                    particle_0_5_enabled INTEGER NOT NULL DEFAULT 1,
                    particle_1_0_max REAL,
                    particle_1_0_enabled INTEGER NOT NULL DEFAULT 0,
                    particle_2_5_max REAL,
                    particle_2_5_enabled INTEGER NOT NULL DEFAULT 0,
                    particle_5_0_max REAL,
                    particle_5_0_enabled INTEGER NOT NULL DEFAULT 0,
                    particle_10_0_max REAL,
                    particle_10_0_enabled INTEGER NOT NULL DEFAULT 0,
                    particle_5_max REAL NOT NULL DEFAULT 100000,
                    temperature_min REAL NOT NULL,
                    temperature_max REAL NOT NULL,
                    humidity_min REAL NOT NULL,
                    humidity_max REAL NOT NULL,
                    alarm_delay_seconds INTEGER NOT NULL DEFAULT 300
                );
                CREATE TABLE IF NOT EXISTS alarm_states (
                    device_id TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'unknown',
                    state TEXT NOT NULL,
                    pending_since REAL,
                    active_since REAL,
                    current_value REAL,
                    PRIMARY KEY(device_id, metric)
                );
                CREATE TABLE IF NOT EXISTS alarm_events (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL DEFAULT 'customer-001',
                    site_id TEXT NOT NULL DEFAULT '',
                    cleanroom_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'unknown',
                    metric TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    trigger_value REAL,
                    peak_value REAL,
                    limit_description TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_alarm_events_room_time
                    ON alarm_events(cleanroom_id, started_at DESC);
                """
            )
            room_columns = {row[1] for row in db.execute("PRAGMA table_info(cleanrooms)")}
            if "customer_id" not in room_columns:
                db.execute(
                    "ALTER TABLE cleanrooms ADD COLUMN customer_id TEXT NOT NULL DEFAULT 'customer-001'"
                )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_cleanrooms_customer_name_lower "
                "ON cleanrooms(customer_id, lower(name))"
            )
            device_columns = {row[1] for row in db.execute("PRAGMA table_info(devices)")}
            if "site_id" not in device_columns:
                db.execute("ALTER TABLE devices ADD COLUMN site_id TEXT")
            if "disabled_at" not in device_columns:
                db.execute("ALTER TABLE devices ADD COLUMN disabled_at REAL")
            if "updated_at" not in device_columns:
                db.execute("ALTER TABLE devices ADD COLUMN updated_at REAL")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_devices_site_enabled ON devices(site_id, enabled)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_active_endpoint "
                "ON devices(COALESCE(site_id,''), host, tcp_port, slave) WHERE enabled = 1"
            )
            alarm_columns = {row[1] for row in db.execute("PRAGMA table_info(alarm_events)")}
            if "customer_id" not in alarm_columns:
                db.execute(
                    "ALTER TABLE alarm_events ADD COLUMN customer_id TEXT NOT NULL DEFAULT 'customer-001'"
                )
            for column, definition in {
                "site_id": "TEXT NOT NULL DEFAULT ''",
                "source": "TEXT NOT NULL DEFAULT 'unknown'",
                "synced_at": "REAL",
                "sync_attempts": "INTEGER NOT NULL DEFAULT 0",
                "sync_error": "TEXT",
                "sync_quarantined_at": "REAL",
            }.items():
                if column not in alarm_columns:
                    db.execute(f"ALTER TABLE alarm_events ADD COLUMN {column} {definition}")
            alarm_state_columns = {row[1] for row in db.execute("PRAGMA table_info(alarm_states)")}
            if "source" not in alarm_state_columns:
                db.execute("ALTER TABLE alarm_states ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown'")
            threshold_columns = {row[1] for row in db.execute("PRAGMA table_info(thresholds)")}
            if "particle_0_5_max" not in threshold_columns:
                db.execute(
                    "ALTER TABLE thresholds ADD COLUMN particle_0_5_max REAL NOT NULL DEFAULT 100000"
                )
                db.execute("UPDATE thresholds SET particle_0_5_max = particle_5_max")
                db.execute(
                    "UPDATE alarm_events SET ended_at = COALESCE(ended_at, ?) WHERE metric = 'particle_5_um'",
                    (time.time(),),
                )
                db.execute("DELETE FROM alarm_states WHERE metric = 'particle_5_um'")
            threshold_columns = {row[1] for row in db.execute("PRAGMA table_info(thresholds)")}
            threshold_additions = {
                "particle_0_3_max": "REAL",
                "particle_0_3_enabled": "INTEGER NOT NULL DEFAULT 0",
                "particle_0_5_enabled": "INTEGER NOT NULL DEFAULT 1",
                "particle_1_0_max": "REAL",
                "particle_1_0_enabled": "INTEGER NOT NULL DEFAULT 0",
                "particle_2_5_max": "REAL",
                "particle_2_5_enabled": "INTEGER NOT NULL DEFAULT 0",
                "particle_5_0_max": "REAL",
                "particle_5_0_enabled": "INTEGER NOT NULL DEFAULT 0",
                "particle_10_0_max": "REAL",
                "particle_10_0_enabled": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, definition in threshold_additions.items():
                if column not in threshold_columns:
                    db.execute(f"ALTER TABLE thresholds ADD COLUMN {column} {definition}")
            columns = {row[1] for row in db.execute("PRAGMA table_info(readings)")}
            additions = {
                "customer_id": "TEXT NOT NULL DEFAULT 'customer-001'",
                "site_id": "TEXT NOT NULL DEFAULT ''",
                "cleanroom_id": "TEXT NOT NULL DEFAULT ''",
                "device_id": "TEXT NOT NULL DEFAULT ''",
                "alarm_status": "TEXT NOT NULL DEFAULT 'NORMAL'",
                "alarm_details_json": "TEXT NOT NULL DEFAULT '[]'",
                "record_uuid": "TEXT",
                "synced_at": "REAL",
                "sync_attempts": "INTEGER NOT NULL DEFAULT 0",
                "sync_error": "TEXT",
                "sync_quarantined_at": "REAL",
                "particle_unit_code": "INTEGER",
                "particle_unit_label": "TEXT",
                "protocol_profile": "TEXT",
            }
            for column, definition in additions.items():
                if column not in columns:
                    db.execute(f"ALTER TABLE readings ADD COLUMN {column} {definition}")
            indexed_columns = {row[1] for row in db.execute("PRAGMA table_info(readings)")}
            if {"customer_id", "cleanroom_id", "device_id", "timestamp"}.issubset(indexed_columns):
                db.execute(
                    """CREATE INDEX IF NOT EXISTS idx_readings_scope_time
                       ON readings(customer_id, cleanroom_id, device_id, timestamp)"""
                )
            db.execute(
                "UPDATE readings SET record_uuid = lower(hex(randomblob(16))) WHERE record_uuid IS NULL OR record_uuid = ''"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_record_uuid ON readings(record_uuid)"
            )
            upload_columns = {row[1] for row in db.execute("PRAGMA table_info(readings)")}
            if {
                "site_id", "customer_id", "source", "synced_at",
                "sync_quarantined_at", "timestamp",
            }.issubset(upload_columns):
                db.execute(
                    """CREATE INDEX IF NOT EXISTS idx_readings_upload_queue
                       ON readings(site_id, customer_id, source, synced_at,
                                   sync_quarantined_at, timestamp)"""
                )
            room_count = db.execute("SELECT COUNT(*) FROM cleanrooms").fetchone()[0]
            if not room_count:
                self._seed_defaults(db)
            db.execute(
                """
                UPDATE readings SET cleanroom_id = COALESCE(
                    (SELECT id FROM cleanrooms
                     WHERE cleanrooms.customer_id = readings.customer_id
                       AND cleanrooms.name = readings.cleanroom LIMIT 1), cleanroom_id)
                WHERE cleanroom_id = ''
                """
            )
            db.execute(
                """
                UPDATE readings SET device_id = COALESCE(
                    (SELECT devices.id FROM devices
                     WHERE devices.cleanroom_id = readings.cleanroom_id
                       AND devices.name = readings.device LIMIT 1), device_id)
                WHERE device_id = ''
                """
            )

    def _seed_defaults(self, db: sqlite3.Connection) -> None:
        rooms = [("room-001", "Cleanroom 1", 1)]
        devices = (
            [("device-001", "room-001", "Device 1", "192.168.2.83", 502, 1, 1)]
            if self.options.demo
            else []
        )
        db.executemany(
            "INSERT INTO cleanrooms(id, name, sort_order, customer_id) VALUES (?, ?, ?, ?)",
            [(room_id, name, order, DEFAULT_CUSTOMER_ID) for room_id, name, order in rooms],
        )
        db.executemany(
            "INSERT INTO devices(id, cleanroom_id, name, host, tcp_port, slave, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?)",
            devices,
        )
        for room_id, _name, _order in rooms:
            db.execute(
                """
                INSERT INTO thresholds(
                    cleanroom_id, profile_name, particle_0_5_max, particle_5_max, temperature_min,
                    temperature_max, humidity_min, humidity_max, alarm_delay_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room_id, DEFAULT_THRESHOLDS["profile_name"],
                    DEFAULT_THRESHOLDS["particle_0_5_max"], DEFAULT_THRESHOLDS["particle_0_5_max"],
                    DEFAULT_THRESHOLDS["temperature_min"], DEFAULT_THRESHOLDS["temperature_max"],
                    DEFAULT_THRESHOLDS["humidity_min"], DEFAULT_THRESHOLDS["humidity_max"],
                    DEFAULT_THRESHOLDS["alarm_delay_seconds"],
                ),
            )

    def configuration(self, customer_id: str | None = None) -> list[dict[str, object]]:
        with self.connect() as db:
            if customer_id:
                room_rows = db.execute(
                    "SELECT * FROM cleanrooms WHERE customer_id = ? ORDER BY sort_order, name",
                    (customer_id,),
                ).fetchall()
            else:
                room_rows = db.execute("SELECT * FROM cleanrooms ORDER BY sort_order, name").fetchall()
            reading_columns = {row[1] for row in db.execute("PRAGMA table_info(readings)")}
            persisted_last_seen = (
                """
                (
                    SELECT MAX(readings.timestamp)
                    FROM readings
                    WHERE readings.customer_id = cleanrooms.customer_id
                      AND readings.device_id = devices.id
                      AND readings.source = 'device'
                )
                """
                if {"timestamp", "customer_id", "device_id", "source"}.issubset(reading_columns)
                else "NULL"
            )
            result: list[dict[str, object]] = []
            for room in room_rows:
                devices = [
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "host": row["host"],
                        "tcpPort": row["tcp_port"],
                        "slave": row["slave"],
                        "enabled": bool(row["enabled"]),
                        "site_id": row["site_id"] or "",
                        "last_seen_at": row["last_seen_at"],
                        "online": bool(
                            row["last_seen_at"]
                            and time.time() - float(row["last_seen_at"]) <= 45
                        ),
                    }
                    for row in db.execute(
                        f"""
                        SELECT devices.*,
                            {persisted_last_seen} AS last_seen_at
                        FROM devices
                        JOIN cleanrooms ON cleanrooms.id = devices.cleanroom_id
                        WHERE devices.cleanroom_id = ? AND devices.enabled = 1
                        ORDER BY devices.sort_order, devices.name
                        """,
                        (room["id"],),
                    )
                ]
                threshold = db.execute(
                    "SELECT * FROM thresholds WHERE cleanroom_id = ?", (room["id"],)
                ).fetchone()
                result.append(
                    {
                        "id": room["id"],
                        "customer_id": room["customer_id"],
                        "name": room["name"],
                        "devices": devices,
                        "thresholds": dict(threshold) if threshold else dict(DEFAULT_THRESHOLDS),
                    }
                )
        return result

    @staticmethod
    def _insert_default_thresholds(db: sqlite3.Connection, room_id: str) -> None:
        values = normalise_alarm_thresholds(DEFAULT_THRESHOLDS)
        db.execute(
            """
            INSERT INTO thresholds(
                cleanroom_id,profile_name,
                particle_0_3_max,particle_0_3_enabled,
                particle_0_5_max,particle_0_5_enabled,
                particle_1_0_max,particle_1_0_enabled,
                particle_2_5_max,particle_2_5_enabled,
                particle_5_0_max,particle_5_0_enabled,
                particle_10_0_max,particle_10_0_enabled,particle_5_max,
                temperature_min,temperature_max,humidity_min,humidity_max,
                alarm_delay_seconds
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                room_id, values["profile_name"],
                values["particle_0_3_max"], int(values["particle_0_3_enabled"]),
                values["particle_0_5_max"], int(values["particle_0_5_enabled"]),
                values["particle_1_0_max"], int(values["particle_1_0_enabled"]),
                values["particle_2_5_max"], int(values["particle_2_5_enabled"]),
                values["particle_5_0_max"], int(values["particle_5_0_enabled"]),
                values["particle_10_0_max"], int(values["particle_10_0_enabled"]),
                values["particle_0_5_max"],
                values["temperature_min"], values["temperature_max"],
                values["humidity_min"], values["humidity_max"],
                values["alarm_delay_seconds"],
            ),
        )

    def create_cleanroom(self, customer_id: str, name: object) -> list[dict[str, object]]:
        """Create one customer-scoped cleanroom with a complete default alarm policy."""
        room_name = str(name or "").strip()
        if not room_name or len(room_name) > 100:
            raise ValueError("Cleanroom name is required and must be 100 characters or fewer")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM cleanrooms WHERE customer_id=? AND lower(name)=lower(?)",
                (customer_id, room_name),
            ).fetchone():
                raise ValueError("Cleanroom name is already in use")
            count = int(db.execute(
                "SELECT COUNT(*) FROM cleanrooms WHERE customer_id=?", (customer_id,)
            ).fetchone()[0])
            if count >= MAX_CLEANROOMS:
                raise ValueError(f"A customer can have at most {MAX_CLEANROOMS} cleanrooms")
            sort_order = int(db.execute(
                "SELECT COALESCE(MAX(sort_order),0)+1 FROM cleanrooms WHERE customer_id=?",
                (customer_id,),
            ).fetchone()[0])
            room_id = f"room-{uuid.uuid4().hex}"
            db.execute(
                "INSERT INTO cleanrooms(id,customer_id,name,sort_order) VALUES(?,?,?,?)",
                (room_id, customer_id, room_name, sort_order),
            )
            self._insert_default_thresholds(db, room_id)
        return self.configuration(customer_id)

    def create_device(
        self,
        customer_id: str,
        cleanroom_id: object,
        name: object,
        host: object,
        tcp_port: object = 502,
        slave: object = 1,
        site_id: object = "",
    ) -> list[dict[str, object]]:
        """Register one device; connectivity is established asynchronously by the collector."""
        room_id = str(cleanroom_id or "").strip()
        device_name = str(name or "").strip()
        address_text = str(host or "").strip()
        if not device_name or len(device_name) > 100:
            raise ValueError("Device name is required and must be 100 characters or fewer")
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError as exc:
            raise ValueError(f"Invalid device IP address: {address_text}") from exc
        if address.version != 4 or not any(address in network for network in PRIVATE_DEVICE_NETWORKS):
            raise ValueError("Device IP must be a private IPv4 address")
        try:
            port = int(tcp_port)
            unit = int(slave)
        except (TypeError, ValueError) as exc:
            raise ValueError("Port and Slave ID must be whole numbers") from exc
        if not 1 <= port <= 65535:
            raise ValueError("Device port must be between 1 and 65535")
        if not 1 <= unit <= 247:
            raise ValueError("Slave ID must be between 1 and 247")
        collector_site_id = str(site_id or "").strip()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM cleanrooms WHERE id=? AND customer_id=?",
                (room_id, customer_id),
            ).fetchone() is None:
                raise ValueError("Cleanroom not found")
            if db.execute(
                """
                SELECT 1 FROM devices
                JOIN cleanrooms ON cleanrooms.id=devices.cleanroom_id
                WHERE cleanrooms.customer_id=? AND devices.enabled=1
                  AND devices.cleanroom_id=? AND lower(devices.name)=lower(?)
                """,
                (customer_id, room_id, device_name),
            ).fetchone():
                raise ValueError("Device name is already in use in this cleanroom")
            if db.execute(
                """
                SELECT 1 FROM devices
                JOIN cleanrooms ON cleanrooms.id=devices.cleanroom_id
                WHERE cleanrooms.customer_id=? AND devices.enabled=1
                  AND COALESCE(devices.site_id,'')=? AND devices.host=?
                  AND devices.tcp_port=? AND devices.slave=?
                """,
                (customer_id, collector_site_id, str(address), port, unit),
            ).fetchone():
                raise ValueError(
                    f"Device connection {address}:{port} / slave {unit} is already configured"
                )
            count = int(db.execute(
                """
                SELECT COUNT(*) FROM devices
                JOIN cleanrooms ON cleanrooms.id=devices.cleanroom_id
                WHERE cleanrooms.customer_id=? AND devices.enabled=1
                """,
                (customer_id,),
            ).fetchone()[0])
            if count >= MAX_ACTIVE_DEVICES:
                raise ValueError(f"A customer can have at most {MAX_ACTIVE_DEVICES} active devices")
            sort_order = int(db.execute(
                "SELECT COALESCE(MAX(sort_order),0)+1 FROM devices WHERE cleanroom_id=? AND enabled=1",
                (room_id,),
            ).fetchone()[0])
            now = time.time()
            db.execute(
                """
                INSERT INTO devices(
                    id,cleanroom_id,site_id,name,host,tcp_port,slave,enabled,
                    sort_order,disabled_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,1,?,NULL,?)
                """,
                (
                    f"device-{uuid.uuid4().hex}", room_id, collector_site_id or None,
                    device_name, str(address), port, unit, sort_order, now,
                ),
            )
        return self.configuration(customer_id)

    def apply_edge_configuration(self, payload: dict[str, object]) -> None:
        customer_id = str(payload.get("customer_id", "")).strip()
        site_id = str(payload.get("site_id", "")).strip()
        rooms = payload.get("rooms")
        if not customer_id or not site_id or not isinstance(rooms, list):
            raise ValueError("Invalid cloud edge configuration")
        active_ids: set[str] = set()
        with self.connect() as db:
            table_names = {
                row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
            }
            if "customers" in table_names:
                db.execute(
                    "INSERT OR IGNORE INTO customers(id,name,created_at) VALUES(?,?,?)",
                    (customer_id, customer_id, time.time()),
                )
            if "users" in table_names:
                db.execute("UPDATE users SET customer_id=?", (customer_id,))
            db.execute(
                "UPDATE devices SET enabled=0 WHERE site_id=? OR site_id IS NULL",
                (site_id,),
            )
            for room_order, room in enumerate(rooms, 1):
                if not isinstance(room, dict):
                    raise ValueError("Invalid cloud cleanroom configuration")
                room_id = str(room.get("id", "")).strip()
                room_name = str(room.get("name", "")).strip()
                if not room_id or not room_name:
                    raise ValueError("Cloud cleanroom id and name are required")
                db.execute(
                    """
                    INSERT INTO cleanrooms(id,customer_id,name,sort_order) VALUES(?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET customer_id=excluded.customer_id,
                        name=excluded.name,sort_order=excluded.sort_order
                    """,
                    (room_id, customer_id, room_name, room_order),
                )
                limits = room.get("thresholds")
                if not isinstance(limits, dict):
                    raise ValueError("Cloud alarm limits are required")
                current_threshold = db.execute(
                    "SELECT * FROM thresholds WHERE cleanroom_id = ?", (room_id,)
                ).fetchone()
                normalised = normalise_alarm_thresholds(
                    limits,
                    dict(current_threshold) if current_threshold else None,
                )
                db.execute(
                    """
                    INSERT INTO thresholds(
                        cleanroom_id,profile_name,
                        particle_0_3_max,particle_0_3_enabled,
                        particle_0_5_max,particle_0_5_enabled,
                        particle_1_0_max,particle_1_0_enabled,
                        particle_2_5_max,particle_2_5_enabled,
                        particle_5_0_max,particle_5_0_enabled,
                        particle_10_0_max,particle_10_0_enabled,particle_5_max,
                        temperature_min,temperature_max,humidity_min,humidity_max,
                        alarm_delay_seconds
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(cleanroom_id) DO UPDATE SET
                        profile_name=excluded.profile_name,
                        particle_0_3_max=excluded.particle_0_3_max,
                        particle_0_3_enabled=excluded.particle_0_3_enabled,
                        particle_0_5_max=excluded.particle_0_5_max,
                        particle_0_5_enabled=excluded.particle_0_5_enabled,
                        particle_1_0_max=excluded.particle_1_0_max,
                        particle_1_0_enabled=excluded.particle_1_0_enabled,
                        particle_2_5_max=excluded.particle_2_5_max,
                        particle_2_5_enabled=excluded.particle_2_5_enabled,
                        particle_5_0_max=excluded.particle_5_0_max,
                        particle_5_0_enabled=excluded.particle_5_0_enabled,
                        particle_10_0_max=excluded.particle_10_0_max,
                        particle_10_0_enabled=excluded.particle_10_0_enabled,
                        particle_5_max=excluded.particle_5_max,
                        temperature_min=excluded.temperature_min,
                        temperature_max=excluded.temperature_max,
                        humidity_min=excluded.humidity_min,
                        humidity_max=excluded.humidity_max,
                        alarm_delay_seconds=excluded.alarm_delay_seconds
                    """,
                    (
                        room_id, normalised["profile_name"],
                        normalised["particle_0_3_max"], int(normalised["particle_0_3_enabled"]),
                        normalised["particle_0_5_max"], int(normalised["particle_0_5_enabled"]),
                        normalised["particle_1_0_max"], int(normalised["particle_1_0_enabled"]),
                        normalised["particle_2_5_max"], int(normalised["particle_2_5_enabled"]),
                        normalised["particle_5_0_max"], int(normalised["particle_5_0_enabled"]),
                        normalised["particle_10_0_max"], int(normalised["particle_10_0_enabled"]),
                        normalised["particle_0_5_max"],
                        normalised["temperature_min"], normalised["temperature_max"],
                        normalised["humidity_min"], normalised["humidity_max"],
                        normalised["alarm_delay_seconds"],
                    ),
                )
                devices = room.get("devices", [])
                if not isinstance(devices, list):
                    raise ValueError("Cloud devices must be a list")
                for device_order, device in enumerate(devices, 1):
                    if not isinstance(device, dict):
                        raise ValueError("Invalid cloud device configuration")
                    device_id = str(device.get("id", "")).strip()
                    name = str(device.get("name", "")).strip()
                    host = str(device.get("host", "")).strip()
                    tcp_port = int(device.get("tcpPort", 502))
                    slave = int(device.get("slave", 1))
                    if not device_id or not name or not host:
                        raise ValueError("Cloud device id, name and host are required")
                    active_ids.add(device_id)
                    db.execute(
                        """
                        INSERT INTO devices(
                            id,cleanroom_id,site_id,name,host,tcp_port,slave,enabled,sort_order
                        ) VALUES(?,?,?,?,?,?,?,1,?)
                        ON CONFLICT(id) DO UPDATE SET
                            cleanroom_id=excluded.cleanroom_id,site_id=excluded.site_id,
                            name=excluded.name,host=excluded.host,tcp_port=excluded.tcp_port,
                            slave=excluded.slave,enabled=1,sort_order=excluded.sort_order
                        """,
                        (device_id,room_id,site_id,name,host,tcp_port,slave,device_order),
                    )
            db.execute("PRAGMA optimize")
        with self._lock:
            self.latest = {key: value for key, value in self.latest.items() if key in active_ids}
            self.last_recorded = {
                key: value for key, value in self.last_recorded.items() if key in active_ids
            }

    def update_customer_settings(self, payload: dict[str, object], customer_id: str) -> None:
        rooms = payload.get("rooms")
        if not isinstance(rooms, list) or not rooms:
            raise ValueError("rooms must be a non-empty array")
        known = {room["id"]: room for room in self.configuration(customer_id)}
        with self.connect() as db:
            seen_room_names: set[str] = set()
            for room_payload in rooms:
                if not isinstance(room_payload, dict) or room_payload.get("id") not in known:
                    raise ValueError("Unknown cleanroom")
                room_id = str(room_payload["id"])
                room_name = str(room_payload.get("name", "")).strip()
                if not room_name or room_name.casefold() in seen_room_names:
                    raise ValueError("Cleanroom names must be non-empty and unique")
                seen_room_names.add(room_name.casefold())
                db.execute("UPDATE cleanrooms SET name = ? WHERE id = ?", (room_name, room_id))

                device_names: set[str] = set()
                devices = room_payload.get("devices", [])
                if not isinstance(devices, list):
                    raise ValueError("devices must be an array")
                for device_payload in devices:
                    if not isinstance(device_payload, dict):
                        raise ValueError("Invalid device")
                    device_id = str(device_payload.get("id", ""))
                    if not any(item["id"] == device_id for item in known[room_id]["devices"]):
                        raise ValueError("Unknown device")
                    name = str(device_payload.get("name", "")).strip()
                    if not name or name.casefold() in device_names:
                        raise ValueError("Device names must be non-empty and unique within a cleanroom")
                    device_names.add(name.casefold())
                    db.execute("UPDATE devices SET name = ? WHERE id = ?", (name, device_id))

                thresholds = room_payload.get("thresholds", {})
                if not isinstance(thresholds, dict):
                    raise ValueError("Invalid thresholds")
                values = normalise_alarm_thresholds(
                    thresholds,
                    known[room_id]["thresholds"],
                )
                db.execute(
                    """
                    UPDATE thresholds SET profile_name = ?,
                        particle_0_3_max = ?, particle_0_3_enabled = ?,
                        particle_0_5_max = ?, particle_0_5_enabled = ?,
                        particle_1_0_max = ?, particle_1_0_enabled = ?,
                        particle_2_5_max = ?, particle_2_5_enabled = ?,
                        particle_5_0_max = ?, particle_5_0_enabled = ?,
                        particle_10_0_max = ?, particle_10_0_enabled = ?,
                        particle_5_max = ?,
                        temperature_min = ?, temperature_max = ?, humidity_min = ?,
                        humidity_max = ?, alarm_delay_seconds = ?
                    WHERE cleanroom_id = ?
                    """,
                    (
                        values["profile_name"],
                        values["particle_0_3_max"], int(values["particle_0_3_enabled"]),
                        values["particle_0_5_max"], int(values["particle_0_5_enabled"]),
                        values["particle_1_0_max"], int(values["particle_1_0_enabled"]),
                        values["particle_2_5_max"], int(values["particle_2_5_enabled"]),
                        values["particle_5_0_max"], int(values["particle_5_0_enabled"]),
                        values["particle_10_0_max"], int(values["particle_10_0_enabled"]),
                        values["particle_0_5_max"],
                        values["temperature_min"], values["temperature_max"],
                        values["humidity_min"], values["humidity_max"],
                        values["alarm_delay_seconds"], room_id,
                    ),
                )

    def update_collector_connections(self, payload: dict[str, object], customer_id: str) -> None:
        """Create, update, and soft-disable on-site devices without changing alarm rules."""
        rooms = payload.get("rooms")
        if not isinstance(rooms, list) or not rooms:
            raise ValueError("rooms must be a non-empty array")
        known = {str(room["id"]): room for room in self.configuration(customer_id)}
        supplied_room_ids = {
            str(room.get("id", "")) for room in rooms if isinstance(room, dict)
        }
        if supplied_room_ids != set(known):
            raise ValueError("Collector cannot add or remove cleanrooms")

        device_count = sum(
            len(room.get("devices", []))
            for room in rooms
            if isinstance(room, dict) and isinstance(room.get("devices"), list)
        )
        if device_count > MAX_ACTIVE_DEVICES:
            raise ValueError(f"A collector can have at most {MAX_ACTIVE_DEVICES} active devices")

        known_by_id = {
            str(device["id"]): (str(room_id), device)
            for room_id, room in known.items()
            for device in room["devices"]
        }
        retained_ids: set[str] = set()
        changed_endpoints: set[str] = set()
        renamed_devices: dict[str, tuple[str, str]] = {}
        rows_to_write: list[tuple[str, int, str, str, str, int, int, str]] = []
        seen_endpoints: set[tuple[str, int, int]] = set()
        seen_device_ids: set[str] = set()

        for room_payload in rooms:
            if not isinstance(room_payload, dict):
                raise ValueError("Invalid cleanroom settings")
            room_id = str(room_payload.get("id", ""))
            devices = room_payload.get("devices")
            if not isinstance(devices, list):
                raise ValueError("devices must be an array")
            seen_names: set[str] = set()
            room_site_id = next(
                (
                    str(device.get("site_id", ""))
                    for device in known[room_id]["devices"]
                    if str(device.get("site_id", ""))
                ),
                "",
            )
            for sort_order, device_payload in enumerate(devices, 1):
                if not isinstance(device_payload, dict):
                    raise ValueError("Invalid device settings")
                device_id = str(device_payload.get("id", "")).strip()
                name = str(device_payload.get("name", "")).strip()
                if not name or len(name) > 100:
                    raise ValueError("Device name is required and must be 100 characters or fewer")
                folded = name.casefold()
                if folded in seen_names:
                    raise ValueError("Device names must be unique within a cleanroom")
                seen_names.add(folded)

                host = str(device_payload.get("host", "")).strip()
                try:
                    address = ipaddress.ip_address(host)
                except ValueError as exc:
                    raise ValueError(f"Invalid device IP address: {host}") from exc
                if address.version != 4 or not any(
                    address in network for network in PRIVATE_DEVICE_NETWORKS
                ):
                    raise ValueError("Device IP must be a private IPv4 address")
                try:
                    tcp_port = int(device_payload.get("tcpPort", 502))
                    slave = int(device_payload.get("slave", 1))
                except (TypeError, ValueError) as exc:
                    raise ValueError("Port and Slave ID must be whole numbers") from exc
                if not 1 <= tcp_port <= 65535:
                    raise ValueError("Device port must be between 1 and 65535")
                if not 1 <= slave <= 247:
                    raise ValueError("Slave ID must be between 1 and 247")

                endpoint = (str(address), tcp_port, slave)
                if endpoint in seen_endpoints:
                    raise ValueError(
                        f"Device connection {endpoint[0]}:{tcp_port} / slave {slave} is already configured"
                    )
                seen_endpoints.add(endpoint)

                if device_id:
                    if device_id in seen_device_ids:
                        raise ValueError("A device can appear only once in the collector configuration")
                    seen_device_ids.add(device_id)
                    known_item = known_by_id.get(device_id)
                    if not known_item or known_item[0] != room_id:
                        raise ValueError("Unknown device or device belongs to another cleanroom")
                    retained_ids.add(device_id)
                    previous = known_item[1]
                    if (
                        str(previous["host"]) != endpoint[0]
                        or int(previous["tcpPort"]) != tcp_port
                        or int(previous["slave"]) != slave
                    ):
                        changed_endpoints.add(device_id)
                    renamed_devices[device_id] = (name, endpoint[0])
                rows_to_write.append(
                    (room_id, sort_order, device_id, name, endpoint[0], tcp_port, slave, room_site_id)
                )

        removed_ids = set(known_by_id) - retained_ids
        now = time.time()
        with self.connect() as db:
            for room_id, sort_order, device_id, name, host, tcp_port, slave, site_id in rows_to_write:
                if device_id:
                    db.execute(
                        """
                        UPDATE devices SET name = ?, host = ?, tcp_port = ?, slave = ?,
                            sort_order = ?, enabled = 1, disabled_at = NULL, updated_at = ?
                        WHERE id = ? AND cleanroom_id = ?
                        """,
                        (name, host, tcp_port, slave, sort_order, now, device_id, room_id),
                    )
                else:
                    db.execute(
                        """
                        INSERT INTO devices(
                            id, cleanroom_id, site_id, name, host, tcp_port, slave,
                            enabled, sort_order, disabled_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, NULL, ?)
                        """,
                        (
                            f"device-{uuid.uuid4().hex}", room_id, site_id, name, host,
                            tcp_port, slave, sort_order, now,
                        ),
                    )
            if removed_ids:
                placeholders = ",".join("?" for _ in removed_ids)
                db.execute(
                    f"UPDATE devices SET enabled = 0, disabled_at = ?, updated_at = ? "
                    f"WHERE id IN ({placeholders})",
                    (now, now, *sorted(removed_ids)),
                )

        for device_id in changed_endpoints | removed_ids:
            self._close_device_client(device_id)
        with self._lock:
            for device_id, (name, host) in renamed_devices.items():
                if device_id in self.latest and device_id not in changed_endpoints:
                    self.latest[device_id]["device"] = name
                    self.latest[device_id]["host"] = host
            for device_id in changed_endpoints | removed_ids:
                self.latest.pop(device_id, None)
                self.last_recorded.pop(device_id, None)
                self._device_failures.pop(device_id, None)
                self._next_poll_at.pop(device_id, None)

    def test_device_connection(self, payload: dict[str, object]) -> dict[str, object]:
        """Read one physical sample without changing the saved device configuration."""
        host = str(payload.get("host", "")).strip()
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError(f"Invalid device IP address: {host}") from exc
        if address.version != 4 or not any(
            address in network for network in PRIVATE_DEVICE_NETWORKS
        ):
            raise ValueError("Device IP must be a private IPv4 address")
        try:
            tcp_port = int(payload.get("tcpPort", 502))
            slave = int(payload.get("slave", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("Port and Slave ID must be whole numbers") from exc
        if not 1 <= tcp_port <= 65535:
            raise ValueError("Device port must be between 1 and 65535")
        if not 1 <= slave <= 247:
            raise ValueError("Slave ID must be between 1 and 247")

        endpoint = (str(address), tcp_port, slave)
        configured_device = next(
            (
                device
                for room in self.configuration()
                for device in room["devices"]
                if (
                    str(device["host"]), int(device["tcpPort"]), int(device["slave"])
                ) == endpoint
            ),
            None,
        )
        started = time.monotonic()
        if configured_device:
            device_id = str(configured_device["id"])
            try:
                with self._io_lock_for_device(device_id):
                    reading = self._client_for_device(configured_device).read_realtime()
            except Exception:
                self._close_device_client(device_id)
                raise
        else:
            client = Dcp8001TcpClient(
                host=endpoint[0], tcp_port=tcp_port, slave=slave,
                timeout=self.options.device_timeout,
            )
            try:
                reading = client.read_realtime()
            finally:
                client.close()
        return {
            "host": str(address),
            "tcpPort": tcp_port,
            "slave": slave,
            "source": "device",
            "latency_ms": max(1, round((time.monotonic() - started) * 1000)),
            "measured_at": reading.timestamp,
            "sample": {
                "particle_0_5_um": reading.particles.get("pm_0_5_um"),
                "temperature": reading.environment.get("temperature"),
                "humidity": reading.environment.get("humidity"),
                "particle_unit": reading.particle_unit_label,
            },
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dcp-monitor", daemon=True)
        self._thread.start()
        mode = "demo" if self.options.demo else "device"
        self.add_log("INFO", "monitor_started", f"Background monitor started in {mode} mode")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(
                timeout=max(
                    2.0,
                    self.options.device_timeout * 2 + 3,
                )
            )
        self._close_all_device_clients()

    def _client_for_device(self, device: dict[str, object]) -> Dcp8001TcpClient:
        device_id = str(device["id"])
        endpoint = (
            str(device["host"]),
            int(device["tcpPort"]),
            int(device["slave"]),
        )
        with self._client_lock:
            cached = self._device_clients.get(device_id)
            if cached and cached[0] == endpoint:
                return cached[1]
            if cached:
                self._device_clients.pop(device_id, None)
        if cached:
            try:
                cached[1].close()
            except OSError:
                pass
        client = Dcp8001TcpClient(
            host=endpoint[0],
            tcp_port=endpoint[1],
            slave=endpoint[2],
            timeout=self.options.device_timeout,
        )
        with self._client_lock:
            self._device_clients[device_id] = (endpoint, client)
        return client

    def _io_lock_for_device(self, device_id: str) -> threading.RLock:
        with self._client_lock:
            return self._device_io_locks.setdefault(device_id, threading.RLock())

    def _close_device_client(self, device_id: str) -> None:
        with self._io_lock_for_device(device_id):
            with self._client_lock:
                cached = self._device_clients.pop(device_id, None)
            if cached:
                try:
                    cached[1].close()
                except OSError:
                    pass

    def _close_all_device_clients(self) -> None:
        with self._client_lock:
            device_ids = list(self._device_clients)
        for device_id in device_ids:
            self._close_device_client(device_id)

    def _run(self) -> None:
        workers = max(1, min(self.options.poll_workers, 32))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dcp-device") as executor:
            in_flight = {}
            while not self._stop.is_set():
                for future in list(in_flight):
                    if future.done():
                        in_flight.pop(future)
                        try:
                            future.result()
                        except Exception as exc:
                            self._log_diagnostic("ERROR", "monitor_worker_failed", str(exc))
                try:
                    configured = [
                        (room, device)
                        for room in self.configuration()
                        for device in room["devices"]
                        if device["enabled"]
                    ]
                except Exception as exc:
                    self._log_diagnostic("ERROR", "monitor_configuration_failed", str(exc))
                    self._stop.wait(1.0)
                    continue
                active_ids = {str(device["id"]) for _room, device in configured}
                busy_ids = set(in_flight.values())
                endpoints = {
                    str(device["id"]): (str(device["host"]), int(device["tcpPort"]), int(device["slave"]))
                    for _room, device in configured
                }
                with self._client_lock:
                    obsolete_ids = [
                        device_id for device_id, (endpoint, _client) in self._device_clients.items()
                        if device_id not in busy_ids and endpoints.get(device_id) != endpoint
                    ]
                for device_id in obsolete_ids:
                    # Disabled/removed registrations must release their session:
                    # embedded gateways may accept only one collection client.
                    self._close_device_client(device_id)
                    with self._lock:
                        self._next_poll_at.pop(device_id, None)
                now = time.monotonic()
                with self._lock:
                    for device_id in set(self._next_poll_at) - active_ids:
                        self._next_poll_at.pop(device_id, None)
                        self._device_failures.pop(device_id, None)
                    scheduled = sorted(
                        (
                            self._next_poll_at.get(str(device["id"]), 0.0),
                            index,
                            room,
                            device,
                        )
                        for index, (room, device) in enumerate(configured)
                        if str(device["id"]) not in busy_ids
                    )
                due = [item for item in scheduled if item[0] <= now][:workers - len(in_flight)]
                for _at, _index, room, device in due:
                    future = executor.submit(self._poll_device, room, device)
                    in_flight[future] = str(device["id"])
                # A slow peer must not hold the next poll of a healthy peer.
                next_due = scheduled[0][0] if scheduled and not due else now + 1.0
                pause = max(0.1, min(1.0, next_due - time.monotonic()))
                if in_flight:
                    wait(in_flight, timeout=pause, return_when=FIRST_COMPLETED)
                else:
                    self._stop.wait(pause)

    def _log_diagnostic(self, level: str, event: str, message: str) -> None:
        try:
            self.add_log(level, event, message)
        except Exception:
            pass  # Operational logging cannot change a physical reading result.

    def _poll_device(self, room: dict[str, object], device: dict[str, object]) -> None:
        with diagnostic_scope(room.get("customer_id"), device.get("site_id")):
            self._poll_device_scoped(room, device)

    def _poll_device_scoped(self, room: dict[str, object], device: dict[str, object]) -> None:
        started = time.monotonic()
        client = None
        device_id = str(device["id"])
        endpoint = f"{device['host']}:{device['tcpPort']}/slave={device['slave']}"
        try:
            if self.options.demo:
                reading = self.demo_factory(str(device["name"]))
                source = "demo"
            else:
                device_id = str(device["id"])
                with self._io_lock_for_device(device_id):
                    client = self._client_for_device(device)
                    reading = client.read_realtime()
                source = "device"
            data = reading if isinstance(reading, dict) else reading.__dict__
            data = dict(data)
            data.update(
                {
                    "cleanroom_id": room["id"],
                    "customer_id": room["customer_id"],
                    "cleanroom": room["name"],
                    "device_id": device["id"],
                    "device": device["name"],
                    "host": device["host"],
                    "site_id": device.get("site_id", ""),
                    "source": source,
                    "online": True,
                    "last_reading_at": data["timestamp"],
                    "connection_checked_at": time.time(),
                }
            )
            changed = self._evaluate_alarms(data, room["thresholds"])
            with self._lock:
                previous = self.latest.get(device_id, {})
                first_read = not previous
                recovered_failures = self._device_failures.get(device_id, 0)
            now = float(data["timestamp"])
            if first_read or recovered_failures or changed or now - self.last_recorded.get(device_id, 0) >= self.options.record_seconds:
                self._persist_sample(data)
            with self._lock:
                self.latest[str(device["id"])] = data
                self._device_failures.pop(str(device["id"]), None)
                self._failure_log_state.pop(device_id, None)
                self._next_poll_at[str(device["id"])] = (
                    time.monotonic() + max(0.1, self.options.poll_seconds)
                )
            if source == "device" and (first_read or recovered_failures):
                self._log_diagnostic("INFO", "device_recovered" if recovered_failures else "device_connected",
                             f"{device['name']}: device={device_id} endpoint={endpoint} "
                             f"read_ms={round((time.monotonic()-started)*1000)} "
                             f"recovered_after_failures={recovered_failures} poll_seconds={self.options.poll_seconds} "
                             f"last_success_at={previous.get('last_reading_at')} recovered_at={now}")
        except Exception as exc:
            device_id = str(device["id"])
            self._close_device_client(device_id)
            with self._lock:
                failure_count = self._device_failures.get(device_id, 0) + 1
                self._device_failures[device_id] = failure_count
                delay = min(
                    min(2.0, max(1.0, self.options.poll_seconds)) * (2 ** min(failure_count - 1, 8)),
                    self.options.offline_backoff_max,
                )
                self._next_poll_at[device_id] = time.monotonic() + delay
                previous = self.latest.get(device_id, {})
                self.latest[device_id] = {
                    **previous,
                    "cleanroom_id": room["id"],
                    "cleanroom": room["name"],
                    "device_id": device_id,
                    "device": device["name"],
                    "host": device["host"],
                    "online": False,
                    "error": str(exc),
                    "timestamp": time.time(),
                    "connection_checked_at": time.time(),
                    "failure_count": failure_count,
                    "retry_at": time.time() + delay,
                }
            # Save the last actually received sample at its original timestamp.
            # Repeated failures must neither duplicate it nor invent outage values.
            if previous.get("online") and previous.get("source") == "device":
                try:
                    self._persist_sample(previous)
                except Exception as storage_exc:
                    self._log_diagnostic("ERROR", "device_sample_save_failed", f"device={device_id}: {storage_exc}")
            # First/change immediately; repeated identical errors summarized every
            # five minutes. Recovery always records the total failed attempts.
            now = time.monotonic()
            previous_error, logged_at, logged_count = self._failure_log_state.get(device_id, ("", 0.0, 0))
            error = str(exc)
            if failure_count == 1 or error != previous_error or now - logged_at >= 300:
                self._failure_log_state[device_id] = (error, now, failure_count)
                self._log_diagnostic("ERROR", "device_offline",
                             f"{device['name']}: {error}; device={device_id} endpoint={endpoint} "
                             f"stage={getattr(client, 'diagnostic_stage', 'connect')} "
                             f"request_sent={getattr(client, 'request_sent', False)} "
                             f"elapsed_ms={round((now-started)*1000)} failures={failure_count} "
                             f"occurrences={failure_count-logged_count if error == previous_error else 1} "
                             f"retry_seconds={delay:g}")

    def _persist_sample(self, data: dict[str, object]) -> None:
        device_id = str(data["device_id"])
        measured_at = float(data["timestamp"])
        if measured_at == self.last_recorded.get(device_id):
            return
        self.record_reading(dict(data), str(data["source"]), str(data["host"]), str(data["cleanroom"]), str(data["device"]))
        self.last_recorded[device_id] = measured_at

    def _metric_checks(
        self,
        data: dict[str, object],
        limits: dict[str, object],
    ) -> list[tuple[str, float, bool, bool, str]]:
        normalised = normalise_alarm_thresholds(limits)
        particles = data["particles"]
        environment = data["environment"]
        temp = float(environment["temperature"])
        humidity = float(environment["humidity"])
        checks: list[tuple[str, float, bool, bool, str]] = []
        for reading_key, metric, max_field, enabled_field, _label in PARTICLE_ALARM_CHANNELS:
            enabled = bool(normalised[enabled_field])
            if enabled and reading_key not in particles:
                raise ValueError(f"Enabled alarm channel {reading_key} is missing from the reading")
            value = float(particles.get(reading_key, 0))
            threshold = normalised[max_field]
            checks.append(
                (
                    metric,
                    value,
                    enabled,
                    enabled and threshold is not None and value > float(threshold),
                    f"> {float(threshold):g} particles/ft³" if threshold is not None else "disabled",
                )
            )
        checks.extend([
            (
                "temperature",
                temp,
                True,
                temp < float(normalised["temperature_min"]) or temp > float(normalised["temperature_max"]),
                f"outside {normalised['temperature_min']}–{normalised['temperature_max']} °C",
            ),
            (
                "humidity",
                humidity,
                True,
                humidity < float(normalised["humidity_min"]) or humidity > float(normalised["humidity_max"]),
                f"outside {normalised['humidity_min']}–{normalised['humidity_max']} %RH",
            ),
        ])
        return checks

    def _evaluate_alarms(self, data: dict[str, object], limits: dict[str, object]) -> bool:
        now = float(data["timestamp"])
        source = str(data.get("source") or "device")
        site_id = str(data.get("site_id") or "")
        delay = int(limits.get("alarm_delay_seconds", 300))
        details: list[dict[str, object]] = []
        pending_logs: list[tuple[str, str, str]] = []
        changed = False
        with self.connect() as db:
            for metric, value, enabled, exceeded, description in self._metric_checks(data, limits):
                row = db.execute(
                    "SELECT * FROM alarm_states WHERE device_id = ? AND metric = ?",
                    (data["device_id"], metric),
                ).fetchone()
                if not enabled:
                    closed_events = db.execute(
                        """UPDATE alarm_events SET ended_at = COALESCE(ended_at, ?), synced_at = NULL
                           WHERE device_id = ? AND metric = ? AND ended_at IS NULL""",
                        (now, data["device_id"], metric),
                    ).rowcount
                    if row is not None:
                        db.execute(
                            "DELETE FROM alarm_states WHERE device_id = ? AND metric = ?",
                            (data["device_id"], metric),
                        )
                    if closed_events or (row is not None and row["state"] != "NORMAL"):
                        changed = True
                        pending_logs.append(
                            (
                                "INFO",
                                "alarm_rule_disabled",
                                f"{data['device']}: {metric} alarm rule disabled",
                            )
                        )
                    continue
                if row and row["source"] != source:
                    db.execute(
                        """UPDATE alarm_events SET ended_at = COALESCE(ended_at, ?)
                           WHERE device_id = ? AND metric = ? AND source = ? AND ended_at IS NULL""",
                        (now, data["device_id"], metric, row["source"]),
                    )
                    row = None
                state = row["state"] if row else "NORMAL"
                pending_since = row["pending_since"] if row else None
                active_since = row["active_since"] if row else None

                if state == "NORMAL" and exceeded:
                    state, pending_since = "PENDING_ALARM", now
                elif state == "PENDING_ALARM":
                    if not exceeded:
                        state, pending_since = "NORMAL", None
                    elif now - float(pending_since) >= delay:
                        state, active_since, pending_since = "ALARM_ACTIVE", now, None
                        event_id = str(uuid.uuid4())
                        db.execute(
                            """
                            INSERT INTO alarm_events(
                                id, customer_id, site_id, cleanroom_id, device_id, source, metric,
                                started_at, trigger_value, peak_value, limit_description
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                event_id, data["customer_id"], site_id, data["cleanroom_id"],
                                data["device_id"], source, metric, now, value, value, description,
                            ),
                        )
                        changed = True
                        pending_logs.append(
                            ("ERROR", "alarm_started", f"{data['device']}: {metric} {description}")
                        )
                elif state == "ALARM_ACTIVE":
                    if exceeded:
                        db.execute(
                            """
                            UPDATE alarm_events SET peak_value = MAX(peak_value, ?), synced_at = NULL
                            WHERE device_id = ? AND metric = ? AND source = ? AND ended_at IS NULL
                            """,
                            (value, data["device_id"], metric, source),
                        )
                    else:
                        state, pending_since = "PENDING_CLEAR", now
                elif state == "PENDING_CLEAR":
                    if exceeded:
                        state, pending_since = "ALARM_ACTIVE", None
                    elif now - float(pending_since) >= delay:
                        state, pending_since, active_since = "NORMAL", None, None
                        db.execute(
                            """
                            UPDATE alarm_events SET ended_at = ?, synced_at = NULL
                            WHERE device_id = ? AND metric = ? AND source = ? AND ended_at IS NULL
                            """,
                            (now, data["device_id"], metric, source),
                        )
                        changed = True
                        pending_logs.append(
                            ("INFO", "alarm_cleared", f"{data['device']}: {metric} returned to normal")
                        )

                db.execute(
                    """
                    INSERT INTO alarm_states(
                        device_id, metric, source, state, pending_since, active_since, current_value
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device_id, metric) DO UPDATE SET state=excluded.state,
                        source=excluded.source,
                        pending_since=excluded.pending_since, active_since=excluded.active_since,
                        current_value=excluded.current_value
                    """,
                    (data["device_id"], metric, source, state, pending_since, active_since, value),
                )
                details.append(
                    {
                        "metric": metric,
                        "value": value,
                        "state": state,
                        "limit": description,
                        "pending_since": pending_since,
                        "active_since": active_since,
                    }
                )
        for level, event, message in pending_logs:
            self.add_log(level, event, message)
        active_states = {"ALARM_ACTIVE", "PENDING_CLEAR"}
        data["alarm_details"] = details
        data["alarm_status"] = "ALARM_ACTIVE" if any(item["state"] in active_states for item in details) else (
            "PENDING" if any(item["state"] == "PENDING_ALARM" for item in details) else "NORMAL"
        )
        return changed

    def latest_for_room(self, customer_id: str, room_id: str | None = None) -> list[dict[str, object]]:
        config = self.configuration(customer_id)
        allowed = {
            device["id"]
            for room in config
            if not room_id or room["id"] == room_id
            for device in room["devices"]
        }
        with self._lock:
            return [dict(value) for key, value in self.latest.items() if key in allowed]

    def latest_all(self) -> list[dict[str, object]]:
        with self._lock:
            return [dict(value) for value in self.latest.values()]

    def alarm_events(self, customer_id: str, room_id: str | None = None, limit: int = 200) -> list[dict[str, object]]:
        limit = max(1, min(limit, 2000))
        with self.connect() as db:
            if room_id:
                rows = db.execute(
                    "SELECT * FROM alarm_events WHERE customer_id = ? AND cleanroom_id = ? ORDER BY started_at DESC LIMIT ?",
                    (customer_id, room_id, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM alarm_events WHERE customer_id = ? ORDER BY started_at DESC LIMIT ?",
                    (customer_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]
