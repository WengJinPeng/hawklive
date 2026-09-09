from __future__ import annotations

import argparse
import csv
import io
import ipaddress
import json
import math
import mimetypes
import os
import platform
import random
import secrets
import socket
import sqlite3
import time
import uuid
import zipfile
from contextlib import contextmanager
from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from auth_service import DEFAULT_CUSTOMER_ID, AuthenticationError, AuthService
from cloud_sync import COLLECTOR_VERSION, CloudSyncService
from connection_status import device_snapshot
from update_protocol import read_json
from collector_settings import (
    load_settings,
    save_settings,
    validate_settings,
)
from dcp8001_collector import Dcp8001TcpClient
from collector_diagnostics import init_queue, enqueue, redact, log_scope, LOCAL_LOG_LIMIT, LOCAL_LOG_SECONDS
from device_discovery import (
    local_private_networks,
    scan_modbus_devices,
    scan_modbus_networks,
)
from monitoring_service import MAX_ACTIVE_DEVICES, MAX_CLEANROOMS, MonitoringService
from report_i18n import (
    normalize_report_locale,
    report_alarm_details,
    report_catalog,
    report_metric_name,
    report_particle_unit,
    report_status,
)
from runtime_paths import migrate_legacy_runtime_data, runtime_paths
from single_instance import AlreadyRunningError, MachineInstanceLock, SingleInstanceLock
from storage_maintenance import LocalStorageManager
from windows_power import SystemSleepInhibitor

POWER_GUARD: SystemSleepInhibitor | None = None

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
RUNTIME_PATHS = runtime_paths()
DB_PATH = RUNTIME_PATHS.database
COLLECTOR_SETTINGS_PATH = RUNTIME_PATHS.collector_settings
DEFAULT_DEVICE_HOST = "192.168.2.30"
DEFAULT_TCP_PORT = 502
DEFAULT_SLAVE = 1
DEFAULT_TIMEOUT = 1.5
DEMO_STARTED_AT = time.time()
MONITOR: MonitoringService | None = None
AUTH: AuthService | None = None
CLOUD_SYNC: CloudSyncService | None = None
HTTP_SERVER: ThreadingHTTPServer | None = None
STORAGE: LocalStorageManager | None = None
COLLECTOR_CSRF_TOKEN = secrets.token_urlsafe(32)
COLLECTOR_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
TOPOLOGY_MANAGER_ROLES = {"admin", "customer_admin", "customer"}


def can_manage_topology(user: dict[str, object] | None) -> bool:
    """Keep legacy `customer` accounts as managers while supporting future read-only roles."""
    return bool(user and str(user.get("role", "")).casefold() in TOPOLOGY_MANAGER_ROLES)


@contextmanager
def connect_db():
    """Open a transactional SQLite connection and always release its file handles."""
    db = sqlite3.connect(DB_PATH, timeout=10)
    try:
        with db:
            yield db
    finally:
        db.close()


def active_collector_customer_id() -> str:
    return CLOUD_SYNC.customer_id if CLOUD_SYNC and CLOUD_SYNC.customer_id else DEFAULT_CUSTOMER_ID


def collector_runtime_snapshot() -> dict[str, object]:
    """Return one safe, reusable health snapshot for local UI and cloud heartbeat."""
    customer_id = active_collector_customer_id()
    rooms = MONITOR.configuration(customer_id) if MONITOR else []
    latest = MONITOR.latest_for_room(customer_id) if MONITOR else []
    devices = [device for room in rooms for device in room.get("devices", [])]
    running = bool(MONITOR and MONITOR._thread and MONITOR._thread.is_alive())
    connection = device_snapshot(devices, latest, running=running,
                                 poll_seconds=MONITOR.options.poll_seconds if MONITOR else 10,
                                 now=time.time())
    successful_times = [
        float(item.get("last_reading_at") or item.get("timestamp") or 0)
        for item in latest
        if item.get("source") == "device"
        and (item.get("last_reading_at") or (not item.get("error") and item.get("timestamp")))
    ]
    errors = [str(item.get("error")) for item in latest if item.get("error")]
    return {
        "sleep_prevention": POWER_GUARD.status() if POWER_GUARD else {"state": "inactive", "system_required": False},
        "update_status": read_json(RUNTIME_PATHS.data_dir / "collector-update-status.json") if os.environ.get("DCP_SUPERVISOR_NONCE") else {"enabled": False, "state": "manual_upgrade_required"},
        "monitor_running": running,
        "poll_seconds": MONITOR.options.poll_seconds if MONITOR else None,
        "record_seconds": MONITOR.options.record_seconds if MONITOR else None,
        **connection,
        "last_reading_at": max(successful_times, default=0) or None,
        "last_error": errors[0][:500] if errors else None,
        "storage": STORAGE.status() if STORAGE else {
            "state": "not_ready", "data_dir": str(RUNTIME_PATHS.data_dir),
        },
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
    }


def collector_host_allowed(host_header: str | None) -> bool:
    if not host_header:
        return False
    try:
        parsed = urlparse(f"//{host_header}")
        _ = parsed.port
    except ValueError:
        return False
    return bool(parsed.hostname and parsed.hostname.casefold() in COLLECTOR_ALLOWED_HOSTS)


def collector_origin_allowed(origin: str | None, host_header: str | None) -> bool:
    if not origin:
        return True
    if not collector_host_allowed(host_header):
        return False
    try:
        expected = urlparse(f"//{host_header}")
        supplied = urlparse(origin)
        expected_port = expected.port or (443 if supplied.scheme == "https" else 80)
        supplied_port = supplied.port or (443 if supplied.scheme == "https" else 80)
    except ValueError:
        return False
    return (
        supplied.scheme in {"http", "https"}
        and supplied.hostname is not None
        and supplied.hostname.casefold() == expected.hostname.casefold()
        and supplied_port == expected_port
        and not supplied.username
        and not supplied.password
    )


def init_db() -> None:
    with connect_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY,
                timestamp REAL NOT NULL,
                source TEXT NOT NULL,
                host TEXT NOT NULL,
                slave INTEGER NOT NULL,
                particles_json TEXT NOT NULL,
                environment_json TEXT NOT NULL,
                alarm_raw INTEGER NOT NULL,
                alarms_json TEXT NOT NULL,
                cleanliness_code INTEGER NOT NULL,
                cleanliness_label TEXT NOT NULL,
                particle_unit_code INTEGER,
                particle_unit_label TEXT,
                protocol_profile TEXT
            )
            """
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(readings)").fetchall()}
        if "cleanroom" not in columns:
            db.execute("ALTER TABLE readings ADD COLUMN cleanroom TEXT NOT NULL DEFAULT 'Cleanroom 1'")
        if "device" not in columns:
            db.execute("ALTER TABLE readings ADD COLUMN device TEXT NOT NULL DEFAULT 'Device 1'")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY,
                timestamp REAL NOT NULL,
                level TEXT NOT NULL,
                event TEXT NOT NULL,
                message TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_readings_timestamp ON readings(timestamp)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_readings_cleanroom_timestamp ON readings(cleanroom, timestamp)")
        columns = {row[1] for row in db.execute("PRAGMA table_info(readings)").fetchall()}
        if {"customer_id", "cleanroom_id", "device_id"}.issubset(columns):
            db.execute(
                """CREATE INDEX IF NOT EXISTS idx_readings_scope_time
                   ON readings(customer_id, cleanroom_id, device_id, timestamp)"""
            )
        init_queue(db)
        db.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)")
        db.execute("PRAGMA optimize")


def add_log(level: str, event: str, message: str) -> None:
    sync = CLOUD_SYNC
    message = redact(message, (sync.token,) if sync else ())
    event = redact(event)[:100]
    with connect_db() as db:
        db.execute(
            "INSERT INTO logs(timestamp, level, event, message) VALUES (?, ?, ?, ?)",
            (time.time(), level, event, message),
        )
        db.execute("DELETE FROM logs WHERE timestamp < ?", (time.time() - LOCAL_LOG_SECONDS,))
        db.execute("DELETE FROM logs WHERE id IN (SELECT id FROM logs ORDER BY id DESC LIMIT -1 OFFSET ?)", (LOCAL_LOG_LIMIT,))
        # Bind at capture time. Never upload another customer's historical logs
        # after re-enrollment; pre-enrollment logs remain local.
        origin = log_scope.get()
        if sync and sync.customer_id and (origin is None or origin == (sync.customer_id, sync.site_id)):
            try:
                enqueue(db, sync.customer_id, sync.site_id, sync.instance_id, level, event, message)
            except sqlite3.Error:
                pass  # Optional diagnostics must not turn a successful read into failure.


def reading_to_dict(reading: object) -> dict[str, object]:
    if isinstance(reading, dict):
        return reading
    return reading.__dict__


def record_reading(reading: object, source: str, host: str, cleanroom: str, device: str) -> dict[str, object]:
    data = reading_to_dict(reading)
    data["cleanroom"] = cleanroom
    data["device"] = device
    data["host"] = host
    data["source"] = source
    with connect_db() as db:
        db.execute(
            """
            INSERT INTO readings(
                timestamp, source, cleanroom, device, host, slave, particles_json, environment_json,
                alarm_raw, alarms_json, cleanliness_code, cleanliness_label,
                customer_id, site_id, cleanroom_id, device_id, alarm_status, alarm_details_json,
                record_uuid, particle_unit_code, particle_unit_label, protocol_profile
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                float(data["timestamp"]),
                source,
                cleanroom,
                device,
                host,
                int(data["slave"]),
                json.dumps(data["particles"]),
                json.dumps(data["environment"]),
                int(data["alarm_raw"]),
                json.dumps(data["alarms"]),
                int(data["cleanliness_code"]),
                str(data["cleanliness_label"]),
                str(data.get("customer_id", "customer-001")),
                str(data.get("site_id", "")),
                str(data.get("cleanroom_id", "")),
                str(data.get("device_id", "")),
                str(data.get("alarm_status", "NORMAL")),
                json.dumps(data.get("alarm_details", [])),
                str(data.get("record_uuid") or uuid.uuid4()),
                data.get("particle_unit_code"),
                data.get("particle_unit_label"),
                data.get("protocol_profile"),
            ),
        )
    return data


def row_to_reading(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "source": row["source"],
        "cleanroom": row["cleanroom"],
        "device": row["device"],
        "host": row["host"],
        "slave": row["slave"],
        "particles": json.loads(row["particles_json"]),
        "environment": json.loads(row["environment_json"]),
        "alarm_raw": row["alarm_raw"],
        "alarms": json.loads(row["alarms_json"]),
        "cleanliness_code": row["cleanliness_code"],
        "cleanliness_label": row["cleanliness_label"],
        "customer_id": row["customer_id"] if "customer_id" in row.keys() else "customer-001",
        "site_id": row["site_id"] if "site_id" in row.keys() else "",
        "cleanroom_id": row["cleanroom_id"] if "cleanroom_id" in row.keys() else "",
        "device_id": row["device_id"] if "device_id" in row.keys() else "",
        "alarm_status": row["alarm_status"] if "alarm_status" in row.keys() else "NORMAL",
        "alarm_details": json.loads(row["alarm_details_json"]) if "alarm_details_json" in row.keys() else [],
        "record_uuid": row["record_uuid"] if "record_uuid" in row.keys() else "",
        "particle_unit_code": row["particle_unit_code"] if "particle_unit_code" in row.keys() else None,
        "particle_unit_label": row["particle_unit_label"] if "particle_unit_label" in row.keys() else None,
        "protocol_profile": row["protocol_profile"] if "protocol_profile" in row.keys() else None,
    }


def list_readings(
    limit: int = 200,
    cleanroom: str | None = None,
    start: float | None = None,
    end: float | None = None,
    customer_id: str | None = None,
    cleanroom_ids: list[str] | None = None,
    device_ids: list[str] | None = None,
) -> list[dict[str, object]]:
    limit = max(1, min(limit, 5000))
    with connect_db() as db:
        db.row_factory = sqlite3.Row
        clauses: list[str] = []
        params: list[object] = []
        if customer_id:
            clauses.append("customer_id = ?")
            params.append(customer_id)
        if cleanroom:
            clauses.append("cleanroom = ?")
            params.append(cleanroom)
        if cleanroom_ids:
            clauses.append(f"cleanroom_id IN ({','.join('?' for _ in cleanroom_ids)})")
            params.extend(cleanroom_ids)
        if device_ids:
            clauses.append(f"device_id IN ({','.join('?' for _ in device_ids)})")
            params.extend(device_ids)
        if start is not None:
            clauses.append("timestamp >= ?")
            params.append(start)
        if end is not None:
            clauses.append("timestamp <= ?")
            params.append(end)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = db.execute(
            f"SELECT * FROM readings {where} ORDER BY timestamp DESC LIMIT ?", params
        ).fetchall()
    return [row_to_reading(row) for row in rows]


def list_trend_readings(
    start: float,
    end: float,
    customer_id: str,
    cleanroom_ids: list[str] | None = None,
    device_ids: list[str] | None = None,
    max_points_per_device: int = 240,
) -> list[dict[str, object]]:
    """Return all sparse rows or an evenly sampled series for each device."""
    if not math.isfinite(start) or not math.isfinite(end) or start >= end:
        return []
    max_points_per_device = max(20, min(int(max_points_per_device), 600))
    clauses = ["customer_id = ?", "timestamp >= ?", "timestamp <= ?"]
    params: list[object] = [customer_id, start, end]
    if cleanroom_ids:
        clauses.append(f"cleanroom_id IN ({','.join('?' for _ in cleanroom_ids)})")
        params.extend(cleanroom_ids)
    if device_ids:
        clauses.append(f"device_id IN ({','.join('?' for _ in device_ids)})")
        params.extend(device_ids)
    where = " AND ".join(clauses)
    with connect_db() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            f"""
            WITH filtered AS (
                SELECT readings.*,
                       COALESCE(NULLIF(device_id, ''), device) AS sample_device_key
                FROM readings
                WHERE {where}
            ),
            sequenced AS (
                SELECT filtered.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY sample_device_key
                           ORDER BY timestamp ASC
                       ) AS sample_sequence,
                       COUNT(*) OVER (
                           PARTITION BY sample_device_key
                       ) AS sample_count
                FROM filtered
            ),
            bucketed AS (
                SELECT sequenced.*,
                       CASE
                           WHEN sample_count <= 1 THEN 0
                           ELSE CAST(
                               ((sample_sequence - 1) * (? - 1)) / (sample_count - 1)
                               AS INTEGER
                           )
                       END AS sample_bucket
                FROM sequenced
            ),
            ranked AS (
                SELECT bucketed.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY sample_device_key, sample_bucket
                           ORDER BY timestamp ASC
                       ) AS bucket_rank
                FROM bucketed
            )
            SELECT * FROM ranked
            WHERE bucket_rank = 1
            ORDER BY timestamp ASC
            """,
            [*params, max_points_per_device],
        ).fetchall()
    return [row_to_reading(row) for row in rows]


def list_cleanrooms(customer_id: str | None = None) -> list[str]:
    with connect_db() as db:
        if customer_id:
            rows = db.execute(
                "SELECT DISTINCT cleanroom FROM readings WHERE customer_id = ? ORDER BY cleanroom COLLATE NOCASE",
                (customer_id,),
            ).fetchall()
        else:
            rows = db.execute("SELECT DISTINCT cleanroom FROM readings ORDER BY cleanroom COLLATE NOCASE").fetchall()
    return [row[0] for row in rows]


def list_logs(limit: int = 200) -> list[dict[str, object]]:
    limit = max(1, min(limit, 2000))
    with connect_db() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM logs ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def db_stats(customer_id: str | None = None) -> dict[str, object]:
    with connect_db() as db:
        if customer_id:
            reading_count = db.execute(
                "SELECT COUNT(*) FROM readings WHERE customer_id = ?", (customer_id,)
            ).fetchone()[0]
            latest = db.execute(
                "SELECT MAX(timestamp) FROM readings WHERE customer_id = ?", (customer_id,)
            ).fetchone()[0]
        else:
            reading_count = db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
            latest = db.execute("SELECT MAX(timestamp) FROM readings").fetchone()[0]
        alarm_count = db.execute(
            "SELECT COUNT(*) FROM alarm_events WHERE customer_id = ?", (customer_id,)
        ).fetchone()[0] if customer_id else db.execute("SELECT COUNT(*) FROM alarm_events").fetchone()[0]
        pending_sync = db.execute(
            """
            SELECT COUNT(*) FROM readings
            WHERE customer_id = ? AND synced_at IS NULL AND cleanroom_id <> '' AND device_id <> ''
            """,
            (customer_id,),
        ).fetchone()[0] if customer_id else 0
    return {
        "database": str(DB_PATH),
        "readings": reading_count,
        "alarms": alarm_count,
        "pending_sync": pending_sync,
        "storage_mode": "On-site SQLite Cache",
        "latest_reading": latest,
    }


def group_readings_by_second(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[int, dict[str, dict[str, object]]] = {}
    for item in rows:
        key = int(float(item["timestamp"]))
        device = str(item["device"])
        groups.setdefault(key, {})
        current = groups[key].get(device)
        if current is None or float(item["timestamp"]) > float(current["timestamp"]):
            groups[key][device] = item
    return [
        {
            "timestamp": key,
            "items": sorted(items.values(), key=lambda item: str(item["device"])),
        }
        for key, items in sorted(groups.items(), reverse=True)
    ]


def safe_sheet_name(name: str, used: set[str], fallback: str = "Cleanroom") -> str:
    cleaned = "".join("_" if ch in r'[]:*?/\\' else ch for ch in name).strip() or fallback
    cleaned = cleaned[:31]
    candidate = cleaned
    index = 2
    while candidate in used:
        suffix = f" {index}"
        candidate = f"{cleaned[:31 - len(suffix)]}{suffix}"
        index += 1
    used.add(candidate)
    return candidate


def xlsx_col(index: int) -> str:
    letters = ""
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def sheet_xml(rows: list[list[object]]) -> str:
    row_xml = []
    for row_index, row in enumerate(rows, 1):
        cells = []
        for col_index, value in enumerate(row, 1):
            cell_ref = f"{xlsx_col(col_index)}{row_index}"
            if isinstance(value, (int, float)) and value != "":
                cells.append(f'<c r="{cell_ref}"><v>{value}</v></c>')
            else:
                text = escape(str(value), quote=True)
                cells.append(f'<c r="{cell_ref}" t="inlineStr"><is><t>{text}</t></is></c>')
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        '<cols>'
        '<col min="1" max="1" width="22" customWidth="1"/>'
        '<col min="2" max="2" width="16" customWidth="1"/>'
        '<col min="3" max="7" width="13" customWidth="1"/>'
        '</cols>'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        '</worksheet>'
    )


def workbook_xlsx(
    cleanroom_rows: dict[str, list[dict[str, object]]], locale: str = "en-US",
) -> bytes:
    locale = normalize_report_locale(locale)
    catalog = report_catalog(locale)

    def numeric(values: list[object]) -> list[float]:
        result: list[float] = []
        for value in values:
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                result.append(float(value))
        return result

    summary = [list(catalog["summary_headers"])]
    particle_unit = report_particle_unit(locale)
    metric_specs = [
        ("0.3 µm", particle_unit, lambda row: row["particles"].get("pm_0_3_um")),
        ("0.5 µm", particle_unit, lambda row: row["particles"].get("pm_0_5_um")),
        ("1.0 µm", particle_unit, lambda row: row["particles"].get("pm_1_0_um")),
        ("2.5 µm", particle_unit, lambda row: row["particles"].get("pm_2_5_um")),
        ("5.0 µm", particle_unit, lambda row: row["particles"].get("pm_5_0_um")),
        ("10.0 µm", particle_unit, lambda row: row["particles"].get("pm_10_0_um")),
        (report_metric_name("temperature", locale), "°C", lambda row: row["environment"].get("temperature")),
        (report_metric_name("humidity", locale), "%RH", lambda row: row["environment"].get("humidity")),
    ]
    room_tables: list[tuple[str, list[list[object]]]] = []
    for cleanroom, rows in cleanroom_rows.items():
        table: list[list[object]] = [list(catalog["table_headers"])]
        for item in sorted(rows, key=lambda row: float(row["timestamp"])):
            particles = item["particles"]
            environment = item["environment"]
            details = report_alarm_details(item.get("alarm_details", []), locale)
            table.append([
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(item["timestamp"]))),
                item["device"], particles.get("pm_0_3_um", ""), particles.get("pm_0_5_um", ""),
                particles.get("pm_1_0_um", ""), particles.get("pm_2_5_um", ""),
                particles.get("pm_5_0_um", ""), particles.get("pm_10_0_um", ""),
                environment.get("temperature", ""), environment.get("humidity", ""),
                report_status(item.get("alarm_status", "NORMAL"), locale), details,
            ])
        room_tables.append((cleanroom, table))

        devices = sorted({str(row["device"]) for row in rows})
        for device in devices:
            device_rows = sorted(
                (row for row in rows if str(row["device"]) == device),
                key=lambda row: float(row["timestamp"]),
            )
            alarm_count = 0
            previous_active = False
            for row in device_rows:
                active = row.get("alarm_status") == "ALARM_ACTIVE"
                if active and not previous_active:
                    alarm_count += 1
                previous_active = active
            start_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(device_rows[0]["timestamp"])))
            end_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(device_rows[-1]["timestamp"])))
            for metric_name, unit, getter in metric_specs:
                values = numeric([getter(row) for row in device_rows])
                if not values:
                    continue
                summary.append([
                    cleanroom, device, metric_name, unit, sum(values) / len(values), min(values), max(values),
                    alarm_count, len(values), start_text, end_text,
                ])

    output = io.BytesIO()
    used_names: set[str] = set()
    fallback = str(catalog["cleanroom_fallback"])
    sheets: list[tuple[str, list[list[object]]]] = [
        (safe_sheet_name(str(catalog["summary_sheet"]), used_names, fallback), summary)
    ]
    sheets.extend((safe_sheet_name(name, used_names, fallback), table) for name, table in room_tables)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for index, _sheet in enumerate(sheets, 1)
            )
            + "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
                for index, _sheet in enumerate(sheets, 1)
            )
            + "</Relationships>",
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets>'
            + "".join(
                f'<sheet name="{escape(name, quote=True)}" sheetId="{index}" r:id="rId{index}"/>'
                for index, (name, _rows) in enumerate(sheets, 1)
            )
            + "</sheets></workbook>",
        )
        for index, (_name, table) in enumerate(sheets, 1):
            zf.writestr(f"xl/worksheets/sheet{index}.xml", sheet_xml(table))
    return output.getvalue()


def parse_int(value: str | None, default: int) -> int:
    if value in (None, ""):
        return default
    return int(value)


def parse_float(value: str | None, default: float) -> float:
    if value in (None, ""):
        return default
    return float(value)


def parse_query_values(
    query: dict[str, list[str]], key: str, singular_key: str | None = None,
) -> list[str]:
    values: list[str] = []
    for raw in [*query.get(key, []), *(query.get(singular_key, []) if singular_key else [])]:
        for value in raw.split(","):
            normalized = value.strip()
            if normalized and normalized not in values:
                values.append(normalized)
    if len(values) > MAX_ACTIVE_DEVICES or any(len(value) > 128 for value in values):
        raise ValueError("Too many or invalid scope identifiers")
    return values


def client_from_query(query: dict[str, list[str]]) -> Dcp8001TcpClient:
    host = query.get("host", [DEFAULT_DEVICE_HOST])[0].strip() or DEFAULT_DEVICE_HOST
    tcp_port = parse_int(query.get("tcp_port", [str(DEFAULT_TCP_PORT)])[0], DEFAULT_TCP_PORT)
    slave = parse_int(query.get("slave", [str(DEFAULT_SLAVE)])[0], DEFAULT_SLAVE)
    timeout = parse_float(query.get("timeout", [str(DEFAULT_TIMEOUT)])[0], DEFAULT_TIMEOUT)
    return Dcp8001TcpClient(host=host, tcp_port=tcp_port, slave=slave, timeout=timeout)


def demo_reading(device: str = "Device 1") -> dict[str, object]:
    elapsed = time.time() - DEMO_STARTED_AT
    offset = (sum(ord(ch) for ch in device) % 17) / 10
    wave = (math.sin(elapsed / 8 + offset) + 1) / 2
    jitter = lambda scale: random.uniform(-scale, scale)
    particles = {
        "pm_0_3_um": max(0, int(145000 + wave * 65000 + jitter(9000))),
        "pm_0_5_um": max(0, int(86000 + wave * 42000 + jitter(6500))),
        "pm_1_0_um": max(0, int(18500 + wave * 9000 + jitter(1800))),
        "pm_2_5_um": max(0, int(6200 + wave * 3100 + jitter(700))),
        "pm_5_0_um": max(0, int(860 + wave * 520 + jitter(120))),
        "pm_10_0_um": max(0, int(180 + wave * 95 + jitter(35))),
    }
    environment = {
        "flow": round(2.83 + jitter(0.018), 3),
        "temperature": round(24.6 + math.sin(elapsed / 30) * 1.1 + jitter(0.12), 2),
        "humidity": round(47.5 + math.cos(elapsed / 25) * 3.2 + jitter(0.25), 2),
        "dew_point": round(12.4 + math.sin(elapsed / 36) * 0.9 + jitter(0.12), 2),
        "wind_speed": round(0.42 + math.sin(elapsed / 18) * 0.08 + jitter(0.02), 3),
        "pressure_diff": round(8.6 + math.cos(elapsed / 22) * 1.4 + jitter(0.2), 2),
    }
    alarms = {
        "pm_0_3_um": particles["pm_0_3_um"] > 205000,
        "pm_0_5_um": particles["pm_0_5_um"] > 125000,
        "pm_1_0_um": False,
        "pm_2_5_um": False,
        "pm_5_0_um": False,
        "pm_10_0_um": False,
    }
    alarm_raw = sum(1 << idx for idx, key in enumerate(alarms) if alarms[key])
    cleanliness_code = 7 if particles["pm_0_5_um"] < 115000 else 8
    return {
        "timestamp": time.time(),
        "slave": DEFAULT_SLAVE,
        "particles": particles,
        "environment": environment,
        "alarm_raw": alarm_raw,
        "alarms": alarms,
        "cleanliness_code": cleanliness_code,
        "cleanliness_label": "CLASS 7" if cleanliness_code == 7 else "CLASS 8",
        "particle_unit_code": 1,
        "particle_unit_label": "PCS/28.3L",
        "protocol_profile": "demo-dpc8001-g-protocol-2025-06-04",
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "HawkHiveMonitor/0.2"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            monitor_running = bool(MONITOR and MONITOR._thread and MONITOR._thread.is_alive())
            storage = STORAGE.status() if STORAGE else {"state": "not_ready", "integrity": "not_checked"}
            self.write_json({
                "ok": monitor_running and storage.get("state") != "critical",
                "data": {
                    "version": COLLECTOR_VERSION,
                    "sleep_prevention": POWER_GUARD.status() if POWER_GUARD else {"state": "inactive", "system_required": False},
                    "supervisor_nonce": os.environ.get("DCP_SUPERVISOR_NONCE"),
                    "monitor_running": monitor_running,
                    "storage_state": storage.get("state"),
                    "database_integrity": storage.get("integrity"),
                },
            }, HTTPStatus.OK if monitor_running and storage.get("state") != "critical" else HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if parsed.path.startswith("/api/collector/"):
            if not self.is_collector_local_request():
                self.write_json({"ok": False, "error": "Local collector access required"}, HTTPStatus.FORBIDDEN)
                return
            collector_customer_id = active_collector_customer_id()
            self.user = {"customer_id": collector_customer_id, "username": "local"}
            if parsed.path == "/api/collector/session":
                self.write_json({"ok": True, "data": {"csrf_token": COLLECTOR_CSRF_TOKEN}})
                return
            if parsed.path == "/api/collector/config":
                self.write_json({"ok": True, "data": MONITOR.configuration(collector_customer_id) if MONITOR else []})
                return
            if parsed.path == "/api/collector/networks":
                self.write_json({"ok": True, "data": local_private_networks()})
                return
            if parsed.path == "/api/collector/cloud":
                settings = load_settings(COLLECTOR_SETTINGS_PATH)
                status = CLOUD_SYNC.connection_status() if CLOUD_SYNC else {
                    "running": False, "connected": False, "state": "not_configured",
                    "last_attempt_at": None, "last_contact_at": None,
                    "last_success_at": None, "last_error": None, "last_error_at": None,
                    "consecutive_failures": 0, "pending_uploads": 0,
                    "quarantined_uploads": 0, "unassigned_uploads": 0,
                    "oldest_pending_at": None,
                    "remote_discovery_enabled": False,
                }
                self.write_json({"ok": True, "data": {**settings.public_dict(), **status}})
                return
            if parsed.path == "/api/collector/status":
                self.handle_collector_status()
                return
            if parsed.path == "/api/collector/logs":
                self.handle_logs(parsed.query)
                return
        if parsed.path.startswith("/api/"):
            user = self.current_user()
            if parsed.path == "/api/session":
                if user:
                    self.write_json({"ok": True, "data": user})
                else:
                    self.write_json({"ok": False, "error": "Authentication required"}, HTTPStatus.UNAUTHORIZED)
                return
            if not user:
                self.write_json({"ok": False, "error": "Authentication required"}, HTTPStatus.UNAUTHORIZED)
                return
            self.user = user
        if parsed.path == "/api/config":
            self.write_json({"ok": True, "data": MONITOR.configuration(str(self.user["customer_id"])) if MONITOR else []})
            return
        if parsed.path == "/api/admin/sites":
            if not self.require_topology_manager():
                return
            self.handle_admin_sites()
            return
        if parsed.path == "/api/admin/discovered-devices":
            if not self.require_topology_manager():
                return
            # Cloud-triggered automatic discovery uploads candidates to the
            # cloud API. The local-only console has no remote candidate inbox.
            self.write_json({"ok": True, "data": []})
            return
        if parsed.path == "/api/admin/pending-collectors":
            if not self.require_topology_manager():
                return
            # Pairing approvals live on the cloud tenant, not the local console.
            self.write_json({"ok": True, "data": []})
            return
        if parsed.path == "/api/collector/status":
            self.handle_collector_status()
            return
        if parsed.path == "/api/collector/logs":
            self.handle_logs(parsed.query)
            return
        if parsed.path == "/api/latest":
            query = parse_qs(parsed.query)
            room_id = query.get("cleanroom_id", [""])[0].strip() or None
            self.write_json({"ok": True, "data": MONITOR.latest_for_room(str(self.user["customer_id"]), room_id) if MONITOR else []})
            return
        if parsed.path == "/api/alarms":
            query = parse_qs(parsed.query)
            room_id = query.get("cleanroom_id", [""])[0].strip() or None
            limit = parse_int(query.get("limit", ["200"])[0], 200)
            self.write_json({"ok": True, "data": MONITOR.alarm_events(str(self.user["customer_id"]), room_id, limit) if MONITOR else []})
            return
        if parsed.path == "/api/history":
            self.handle_history(parsed.query)
            return
        if parsed.path == "/api/trends":
            self.handle_trends(parsed.query)
            return
        if parsed.path == "/api/history/export":
            self.handle_export(parsed.query)
            return
        if parsed.path == "/api/history/export-xlsx":
            self.handle_export_xlsx(parsed.query)
            return
        if parsed.path == "/api/stats":
            self.write_json({"ok": True, "data": db_stats(str(self.user["customer_id"]))})
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/collector/"):
            if not self.authorize_collector_mutation():
                return
            collector_customer_id = active_collector_customer_id()
            self.user = {"customer_id": collector_customer_id, "username": "local"}
            if parsed.path == "/api/collector/config":
                self.handle_config_update(collector_customer_id)
                return
            if parsed.path == "/api/collector/discover":
                self.handle_collector_discovery()
                return
            if parsed.path == "/api/collector/test-device":
                self.handle_collector_device_test()
                return
            if parsed.path == "/api/collector/cloud":
                self.handle_collector_cloud_update()
                return
        if parsed.path == "/api/login":
            self.handle_login()
            return
        user = self.current_user()
        if not user:
            self.write_json({"ok": False, "error": "Authentication required"}, HTTPStatus.UNAUTHORIZED)
            return
        self.user = user
        if parsed.path == "/api/logout":
            self.handle_logout()
            return
        if parsed.path == "/api/config":
            if not self.require_topology_manager():
                return
            self.handle_config_update()
            return
        if parsed.path == "/api/admin/cleanrooms":
            if not self.require_topology_manager():
                return
            self.handle_admin_cleanroom_create()
            return
        if parsed.path == "/api/admin/devices":
            if not self.require_topology_manager():
                return
            self.handle_admin_device_create()
            return
        if parsed.path == "/api/collector/discover":
            if not self.require_topology_manager():
                return
            self.handle_collector_discovery()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def auth_token(self) -> str | None:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get("dcp_session")
        return morsel.value if morsel else None

    def current_user(self) -> dict[str, object] | None:
        return AUTH.user_for_token(self.auth_token()) if AUTH else None

    def require_topology_manager(self) -> bool:
        if can_manage_topology(getattr(self, "user", None)):
            return True
        self.write_json(
            {"ok": False, "error": "Administrator permission is required"},
            HTTPStatus.FORBIDDEN,
        )
        return False

    def handle_admin_sites(self) -> None:
        runtime = collector_runtime_snapshot()
        storage = runtime.get("storage") if isinstance(runtime.get("storage"), dict) else {}
        if CLOUD_SYNC:
            status = CLOUD_SYNC.connection_status()
            desired_revision = status.get("desired_config_version")
            applied_revision = status.get("applied_config_version")
            apply_status = str(status.get("config_apply_status") or "awaiting")
            if apply_status == "failed":
                config_state = "failed"
            elif (
                applied_revision is not None and desired_revision is not None
                and int(applied_revision) >= int(desired_revision)
            ):
                config_state = "applied"
            else:
                config_state = "pending"
            sites = [{
                "id": CLOUD_SYNC.site_id,
                "name": CLOUD_SYNC.site_name or f"本机采集器 · {CLOUD_SYNC.site_id}",
                "is_current": True,
                "connected": True,
                "connection_state": "online",
                "last_contact_at": status.get("last_contact_at"),
                "last_heartbeat_at": status.get("last_heartbeat_at"),
                "config_version": desired_revision,
                "applied_config_version": applied_revision,
                "config_apply_status": apply_status,
                "config_apply_error": status.get("config_apply_error"),
                "config_state": config_state,
                "version": COLLECTOR_VERSION,
                "hostname": runtime.get("hostname"),
                "platform": runtime.get("platform"),
                "monitor_running": runtime.get("monitor_running"),
                "device_total": runtime.get("device_total", 0),
                "device_online": runtime.get("device_online", 0),
                "device_offline": runtime.get("device_offline", 0),
                "device_unknown": runtime.get("device_unknown", 0),
                "device_states": runtime.get("device_states", []),
                "last_reading_at": runtime.get("last_reading_at"),
                "pending_uploads": status.get("pending_uploads", 0),
                "quarantined_uploads": status.get("quarantined_uploads", 0),
                "unassigned_uploads": status.get("unassigned_uploads", 0),
                "oldest_pending_at": status.get("oldest_pending_at"),
                "storage_state": storage.get("state", "unknown"),
                "disk_free_bytes": storage.get("disk_free_bytes"),
                "last_error": status.get("last_error") or runtime.get("last_error"),
            }]
        else:
            sites = [{
                "id": "local",
                "name": "本机采集器",
                "is_current": True,
                "connected": True,
                "connection_state": "online",
                "last_contact_at": None,
                "last_heartbeat_at": None,
                "config_version": None,
                "applied_config_version": None,
                "config_apply_status": "applied",
                "config_apply_error": None,
                "config_state": "local",
                "version": COLLECTOR_VERSION,
                "hostname": runtime.get("hostname"),
                "platform": runtime.get("platform"),
                "monitor_running": runtime.get("monitor_running"),
                "device_total": runtime.get("device_total", 0),
                "device_online": runtime.get("device_online", 0),
                "device_offline": runtime.get("device_offline", 0),
                "device_unknown": runtime.get("device_unknown", 0),
                "device_states": runtime.get("device_states", []),
                "last_reading_at": runtime.get("last_reading_at"),
                "pending_uploads": 0,
                "quarantined_uploads": 0,
                "unassigned_uploads": 0,
                "oldest_pending_at": None,
                "storage_state": storage.get("state", "unknown"),
                "disk_free_bytes": storage.get("disk_free_bytes"),
                "last_error": runtime.get("last_error"),
            }]
        self.write_json({
            "ok": True,
            "data": {
                "sites": sites,
                "max_cleanrooms": MAX_CLEANROOMS,
                "max_active_devices": MAX_ACTIVE_DEVICES,
                "max_collectors": 1,
                "manual_registration": True,
                "connection_verification": "collector",
                "management_scope": "current_collector",
                "can_create_collectors": False,
                "can_download_collector": False,
                "scope_notice": "这里管理的是本机采集器；其他远程采集器请在云端工作台添加和分配设备。",
            },
        })

    def handle_admin_cleanroom_create(self) -> None:
        if MONITOR is None:
            self.write_json({"ok": False, "error": "Monitor is not ready"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        try:
            payload = self.read_json_body()
            name = str(payload.get("name", "")).strip()
            customer_id = str(self.user["customer_id"])
            if CLOUD_SYNC:
                rooms = CLOUD_SYNC.create_cleanroom_once(name)
            else:
                rooms = MONITOR.create_cleanroom(customer_id, name)
            add_log(
                "INFO", "cleanroom_created",
                f"{self.user.get('username', 'administrator')} created cleanroom {name}",
            )
            self.write_json({"ok": True, "data": rooms}, HTTPStatus.CREATED)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.write_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            add_log("ERROR", "cleanroom_create_failed", str(exc))
            self.write_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_GATEWAY)

    def handle_admin_device_create(self) -> None:
        if MONITOR is None:
            self.write_json({"ok": False, "error": "Monitor is not ready"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        try:
            payload = self.read_json_body()
            customer_id = str(self.user["customer_id"])
            requested_site_id = str(payload.get("site_id", "")).strip()
            if CLOUD_SYNC:
                if requested_site_id and requested_site_id != CLOUD_SYNC.site_id:
                    raise ValueError("Selected site does not belong to this collector")
                payload["site_id"] = CLOUD_SYNC.site_id
                rooms = CLOUD_SYNC.create_device_once(payload)
            else:
                if requested_site_id not in {"", "local"}:
                    raise ValueError("Selected site is not available on this server")
                rooms = MONITOR.create_device(
                    customer_id,
                    payload.get("cleanroom_id"),
                    payload.get("name"),
                    payload.get("host"),
                    payload.get("tcp_port", 502),
                    payload.get("slave", 1),
                    "",
                )
            add_log(
                "INFO", "device_registered",
                f"{self.user.get('username', 'administrator')} manually registered "
                f"{str(payload.get('name', '')).strip()}",
            )
            self.write_json({"ok": True, "data": rooms}, HTTPStatus.CREATED)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.write_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            add_log("ERROR", "device_register_failed", str(exc))
            self.write_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_GATEWAY)

    def is_loopback_request(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def is_collector_local_request(self) -> bool:
        return self.is_loopback_request() and collector_host_allowed(self.headers.get("Host"))

    def authorize_collector_mutation(self) -> bool:
        if not self.is_collector_local_request():
            self.write_json({"ok": False, "error": "Local collector access required"}, HTTPStatus.FORBIDDEN)
            return False
        if self.headers.get_content_type().casefold() != "application/json":
            self.write_json({"ok": False, "error": "Content-Type must be application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return False
        if not collector_origin_allowed(self.headers.get("Origin"), self.headers.get("Host")):
            self.write_json({"ok": False, "error": "Cross-origin collector changes are not allowed"}, HTTPStatus.FORBIDDEN)
            return False
        supplied = self.headers.get("X-DCP-CSRF", "")
        if not secrets.compare_digest(supplied, COLLECTOR_CSRF_TOKEN):
            self.write_json({"ok": False, "error": "Collector request token is invalid"}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def handle_login(self) -> None:
        if AUTH is None:
            self.write_json({"ok": False, "error": "Authentication is not ready"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        try:
            payload = self.read_json_body()
            token, user = AUTH.login(str(payload.get("username", "")), str(payload.get("password", "")))
            secure = "; Secure" if os.environ.get("DASHBOARD_SECURE_COOKIE") == "1" else ""
            self.write_json(
                {"ok": True, "data": user},
                headers={"Set-Cookie": f"dcp_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200{secure}"},
            )
        except (AuthenticationError, ValueError, json.JSONDecodeError) as exc:
            self.write_json({"ok": False, "error": str(exc)}, HTTPStatus.UNAUTHORIZED)

    def handle_logout(self) -> None:
        if AUTH:
            AUTH.logout(self.auth_token())
        self.write_json(
            {"ok": True},
            headers={"Set-Cookie": "dcp_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"},
        )

    def read_json_body(self) -> dict[str, object]:
        length = parse_int(self.headers.get("Content-Length"), 0)
        if length <= 0 or length > 1_000_000:
            raise ValueError("Invalid request body")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def handle_config_update(self, customer_id: str | None = None) -> None:
        if MONITOR is None:
            self.write_json({"ok": False, "error": "Monitor is not ready"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        try:
            selected_customer_id = customer_id or str(self.user["customer_id"])
            payload = self.read_json_body()
            is_collector_update = customer_id is not None
            if is_collector_update and CLOUD_SYNC and CLOUD_SYNC.customer_id == selected_customer_id:
                rooms = CLOUD_SYNC.push_config_once(payload)
            elif is_collector_update:
                MONITOR.update_collector_connections(payload, selected_customer_id)
                rooms = MONITOR.configuration(selected_customer_id)
            elif CLOUD_SYNC and CLOUD_SYNC.customer_id == selected_customer_id:
                rooms = CLOUD_SYNC.push_config_once(
                    self.safe_customer_payload_for_cloud(payload, selected_customer_id)
                )
            else:
                MONITOR.update_customer_settings(payload, selected_customer_id)
                rooms = MONITOR.configuration(selected_customer_id)
            if is_collector_update:
                add_log("INFO", "collector_connections", "On-site device connection parameters updated")
            else:
                add_log("INFO", "customer_settings", "Cleanroom names, device names and alarm thresholds updated")
            self.write_json({"ok": True, "data": rooms})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.write_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def safe_customer_payload_for_cloud(
        self, payload: dict[str, object], customer_id: str
    ) -> dict[str, object]:
        """Preserve collector-owned connection fields in customer-originated updates."""
        requested_rooms = payload.get("rooms")
        if not isinstance(requested_rooms, list):
            raise ValueError("rooms must be an array")
        known_rooms = {
            str(room["id"]): room for room in MONITOR.configuration(customer_id)
        }
        requested_by_id = {
            str(room.get("id", "")): room
            for room in requested_rooms
            if isinstance(room, dict)
        }
        if set(requested_by_id) != set(known_rooms):
            raise ValueError("Cleanroom list cannot be changed by a customer")

        safe_rooms: list[dict[str, object]] = []
        for room_id, known_room in known_rooms.items():
            requested_room = requested_by_id[room_id]
            requested_devices = requested_room.get("devices")
            if not isinstance(requested_devices, list):
                raise ValueError("devices must be an array")
            requested_device_by_id = {
                str(device.get("id", "")): device
                for device in requested_devices
                if isinstance(device, dict)
            }
            known_devices = {
                str(device["id"]): device for device in known_room.get("devices", [])
            }
            if set(requested_device_by_id) != set(known_devices):
                raise ValueError("Device list cannot be changed by a customer")
            safe_devices = [
                {
                    **known_device,
                    "name": str(
                        requested_device_by_id[device_id].get(
                            "name", known_device["name"]
                        )
                    ).strip(),
                }
                for device_id, known_device in known_devices.items()
            ]
            safe_rooms.append(
                {
                    **known_room,
                    "name": str(requested_room.get("name", known_room["name"])).strip(),
                    "devices": safe_devices,
                    "thresholds": requested_room.get(
                        "thresholds", known_room.get("thresholds", {})
                    ),
                }
            )
        return {"rooms": safe_rooms}

    def handle_read(self, raw_query: str) -> None:
        query = parse_qs(raw_query)
        host = query.get("host", [DEFAULT_DEVICE_HOST])[0].strip() or DEFAULT_DEVICE_HOST
        cleanroom = query.get("cleanroom", ["Cleanroom 1"])[0].strip() or "Cleanroom 1"
        device = query.get("device", ["Device 1"])[0].strip() or "Device 1"
        if query.get("demo", ["0"])[0] == "1":
            data = record_reading(demo_reading(device), "demo", host, cleanroom, device)
            self.write_json({"ok": True, "demo": True, "data": data})
            return
        try:
            with client_from_query(query) as client:
                reading = client.read_realtime()
            data = record_reading(reading, "device", host, cleanroom, device)
            self.write_json({"ok": True, "data": data})
        except Exception as exc:
            add_log("ERROR", "read_failed", str(exc))
            self.write_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_GATEWAY)

    def handle_history(self, raw_query: str) -> None:
        try:
            query = parse_qs(raw_query)
            limit = parse_int(query.get("limit", ["200"])[0], 200)
            cleanroom = query.get("cleanroom", [""])[0].strip() or None
            start = parse_float(query.get("start", [""])[0], 0) or None
            end = parse_float(query.get("end", [""])[0], 0) or None
            self.write_json({
                "ok": True,
                "data": list_readings(
                    limit, cleanroom, start, end, str(self.user["customer_id"]),
                    parse_query_values(query, "cleanroom_ids", "cleanroom_id"),
                    parse_query_values(query, "device_ids", "device_id"),
                ),
            })
        except (ValueError, TypeError) as exc:
            self.write_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def handle_trends(self, raw_query: str) -> None:
        try:
            query = parse_qs(raw_query)
            end = parse_float(query.get("end", [""])[0], time.time())
            start = parse_float(query.get("start", [""])[0], end - 3600)
            max_points = parse_int(query.get("max_points", ["240"])[0], 240)
            rows = list_trend_readings(
                start,
                end,
                str(self.user["customer_id"]),
                parse_query_values(query, "cleanroom_ids", "cleanroom_id"),
                parse_query_values(query, "device_ids", "device_id"),
                max_points,
            )
            self.write_json({"ok": True, "data": rows})
        except (ValueError, TypeError, sqlite3.DatabaseError) as exc:
            self.write_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def handle_logs(self, raw_query: str) -> None:
        query = parse_qs(raw_query)
        limit = parse_int(query.get("limit", ["200"])[0], 200)
        self.write_json({"ok": True, "data": list_logs(limit)})

    def handle_collector_status(self) -> None:
        customer_id = str(self.user["customer_id"])
        rooms = MONITOR.configuration(customer_id) if MONITOR else []
        latest = MONITOR.latest_for_room(customer_id) if MONITOR else []
        devices = [device for room in rooms for device in room.get("devices", [])]
        latest_by_id = {str(item.get("device_id")): item for item in latest}
        device_states: list[dict[str, object]] = []
        for room in rooms:
            for device in room.get("devices", []):
                device_id = str(device.get("id"))
                item = latest_by_id.get(device_id, {})
                online = item.get("online") is True and not item.get("error")
                last_success = item.get("last_reading_at")
                if not last_success and online:
                    last_success = item.get("timestamp")
                device_states.append({
                    "id": device_id,
                    "name": device.get("name"),
                    "cleanroom_id": room.get("id"),
                    "cleanroom": room.get("name"),
                    "online": online,
                    "last_reading_at": last_success,
                    "source": item.get("source"),
                    "alarm_status": item.get("alarm_status", "NORMAL"),
                    "error": item.get("error"),
                    "failure_count": item.get("failure_count", 0),
                    "retry_at": item.get("retry_at"),
                })
        online_ids = {
            str(item.get("device_id"))
            for item in latest
            if item.get("online") is True and not item.get("error")
        }
        last_reading = max(
            (float(item.get("last_reading_at") or item.get("timestamp", 0))
             for item in latest if item.get("online") is True and not item.get("error")),
            default=0,
        ) or None
        cloud_enabled = CLOUD_SYNC is not None
        cloud_status = CLOUD_SYNC.connection_status() if CLOUD_SYNC else {
            "running": False, "connected": False, "state": "not_configured",
            "pending_uploads": 0, "quarantined_uploads": 0, "unassigned_uploads": 0,
            "oldest_pending_at": None, "last_contact_at": None,
        }
        with connect_db() as db:
            if cloud_enabled and CLOUD_SYNC.customer_id:
                row = db.execute(
                    """SELECT MAX(synced_at) FROM readings
                       WHERE customer_id = ? AND site_id = ? AND source = 'device'""",
                    (CLOUD_SYNC.customer_id, CLOUD_SYNC.site_id),
                ).fetchone()
            elif cloud_enabled:
                row = db.execute(
                    """SELECT MAX(synced_at) FROM readings
                       WHERE site_id = ? AND source = 'device'""",
                    (CLOUD_SYNC.site_id,),
                ).fetchone()
            else:
                row = (None,)
        self.write_json({"ok": True, "data": {
            "mode": "demo" if MONITOR and MONITOR.options.demo else "device",
            "monitor_running": bool(MONITOR and MONITOR._thread and MONITOR._thread.is_alive()),
            "record_seconds": MONITOR.options.record_seconds if MONITOR else None,
            "cloud_configured": cloud_enabled,
            "cloud_running": cloud_status["running"],
            "cloud_connected": cloud_status["connected"],
            "cloud_state": cloud_status["state"],
            "cloud_url": CLOUD_SYNC.base_url if cloud_enabled else None,
            "site_id": CLOUD_SYNC.site_id if cloud_enabled else None,
            "device_total": len(devices),
            "device_online": len(online_ids),
            "devices": device_states,
            "last_reading_at": last_reading,
            "last_upload_at": row[0],
            "last_cloud_contact_at": cloud_status["last_contact_at"],
            "pending_uploads": cloud_status["pending_uploads"],
            "quarantined_uploads": cloud_status["quarantined_uploads"],
            "unassigned_uploads": cloud_status["unassigned_uploads"],
            "oldest_pending_at": cloud_status["oldest_pending_at"],
            "poll_seconds": MONITOR.options.poll_seconds if MONITOR else None,
            "poll_workers": MONITOR.options.poll_workers if MONITOR else None,
            "max_active_devices": MAX_ACTIVE_DEVICES,
            "storage": STORAGE.status() if STORAGE else {
                "state": "not_ready", "data_dir": str(RUNTIME_PATHS.data_dir),
            },
        }})

    def handle_collector_discovery(self) -> None:
        try:
            payload = self.read_json_body()
            customer_id = active_collector_customer_id()
            configuration = MONITOR.configuration(customer_id) if MONITOR is not None else []
            requested_cidr = str(payload.get("cidr", "")).strip() or None
            tcp_port = parse_int(payload.get("tcp_port"), DEFAULT_TCP_PORT)
            if not 1 <= tcp_port <= 65535:
                raise ValueError("TCP port must be between 1 and 65535")
            route_target = CLOUD_SYNC.base_url if CLOUD_SYNC else "https://1.1.1.1"
            configured_devices = [
                device
                for room in configuration
                for device in room.get("devices", [])
                if int(device.get("tcpPort", DEFAULT_TCP_PORT)) == tcp_port
            ]
            scanned_cidrs, results = scan_modbus_networks(
                requested_cidr,
                tcp_port,
                route_target,
                excluded_hosts={str(device.get("host", "")) for device in configured_devices},
            )
            scanned_networks = [ipaddress.ip_network(cidr) for cidr in scanned_cidrs]
            latest_by_id = {
                str(item.get("device_id")): item
                for item in (MONITOR.latest_for_room(customer_id) if MONITOR is not None else [])
            }
            for device in configured_devices:
                try:
                    address = ipaddress.ip_address(str(device.get("host", "")))
                except ValueError:
                    continue
                if not any(address in network for network in scanned_networks):
                    continue
                latest = latest_by_id.get(str(device.get("id")), {})
                results.append({
                    "host": str(address),
                    "tcp_port": tcp_port,
                    "slave": int(device.get("slave", 1)),
                    "verified": (
                        latest.get("online") is True
                        and latest.get("source") == "device"
                        and latest.get("particle_unit_code") == 1
                    ),
                    "modbus_responded": latest.get("online") is True and latest.get("source") == "device",
                    "protocol_compatible": latest.get("online") is True and latest.get("source") == "device",
                    "identity_verified": False,
                    "firmware_raw": None,
                    "particle_unit_code": latest.get("particle_unit_code"),
                    "particle_unit_label": latest.get("particle_unit_label"),
                    "unit_supported": latest.get("particle_unit_code") == 1,
                    "latency_ms": None,
                    "configured": True,
                })
            results.sort(key=lambda item: (int(ipaddress.ip_address(str(item["host"]))), int(item["slave"])))
            scanned_label = ", ".join(scanned_cidrs)
            add_log("INFO", "device_discovery_completed", f"Scanned {scanned_label}; found {len(results)} candidate(s)")
            self.write_json({"ok": True, "data": {
                "cidr": scanned_cidrs[0] if len(scanned_cidrs) == 1 else "",
                "cidrs": scanned_cidrs,
                "results": results,
            }})
        except (ValueError, OSError) as exc:
            add_log("ERROR", "device_discovery_failed", str(exc))
            self.write_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def handle_collector_device_test(self) -> None:
        if MONITOR is None:
            self.write_json({"ok": False, "error": "Monitor is not ready"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        try:
            result = MONITOR.test_device_connection(self.read_json_body())
            add_log(
                "INFO", "device_connection_tested",
                f"Verified {result['host']}:{result['tcpPort']} / slave {result['slave']}",
            )
            self.write_json({"ok": True, "data": result})
        except (ValueError, OSError, TimeoutError, ConnectionError) as exc:
            add_log("ERROR", "device_connection_test_failed", str(exc))
            self.write_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_GATEWAY)

    def handle_collector_cloud_update(self) -> None:
        global CLOUD_SYNC
        try:
            payload = self.read_json_body()
            current = load_settings(COLLECTOR_SETTINGS_PATH)
            supplied_token = str(payload.get("token", "")).strip()
            settings = validate_settings(
                str(payload.get("cloud_url", "")),
                str(payload.get("site_id", "")),
                supplied_token or current.token,
            )
            candidate = CloudSyncService(
                DB_PATH, settings.cloud_url, settings.token, settings.site_id, add_log
            )
            if MONITOR:
                candidate.latest_provider = MONITOR.latest_all
                candidate.config_applier = MONITOR.apply_edge_configuration
                candidate.status_provider = collector_runtime_snapshot
            candidate.pull_config_once()
            save_settings(COLLECTOR_SETTINGS_PATH, settings)
            if MONITOR:
                MONITOR.options.demo = False
                with MONITOR._lock:
                    MONITOR.latest.clear()
            if CLOUD_SYNC:
                CLOUD_SYNC.stop()
            CLOUD_SYNC = candidate
            CLOUD_SYNC.start()
            add_log("INFO", "cloud_connected", f"Collector connected as {settings.site_id}")
            self.write_json({"ok": True, "data": {
                **settings.public_dict(), **CLOUD_SYNC.connection_status(), "tested_at": time.time(),
            }})
        except Exception as exc:
            add_log("ERROR", "cloud_connection_failed", str(exc))
            self.write_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_GATEWAY)

    def handle_clear(self, table: str) -> None:
        with connect_db() as db:
            db.execute(f"DELETE FROM {table}")
        add_log("INFO", "clear", f"Cleared {table}")
        self.write_json({"ok": True})

    def handle_export(self, raw_query: str) -> None:
        query = parse_qs(raw_query)
        locale = normalize_report_locale(query.get("locale", ["en-US"])[0])
        catalog = report_catalog(locale)
        rows = list_readings(
            parse_int(query.get("limit", ["5000"])[0], 5000),
            query.get("cleanroom", [""])[0].strip() or None,
            parse_float(query.get("start", [""])[0], 0) or None,
            parse_float(query.get("end", [""])[0], 0) or None,
            customer_id=str(self.user["customer_id"]),
            cleanroom_ids=parse_query_values(query, "cleanroom_ids", "cleanroom_id"),
            device_ids=parse_query_values(query, "device_ids", "device_id"),
        )
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(catalog["csv_headers"])
        for group in group_readings_by_second(rows):
            for index, item in enumerate(group["items"]):
                particles = item["particles"]
                environment = item["environment"]
                writer.writerow(
                    [
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(group["timestamp"])) if index == 0 else "",
                        item["device"],
                        particles.get("pm_0_3_um"),
                        particles.get("pm_0_5_um"),
                        particles.get("pm_1_0_um"),
                        environment.get("temperature"),
                        environment.get("humidity"),
                    ]
                )
        body = output.getvalue().encode("utf-8-sig")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header(
            "Content-Disposition", f"attachment; filename=hawkhive-history-{locale}.csv",
        )
        self.send_header("Content-Language", locale)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def handle_export_xlsx(self, raw_query: str) -> None:
        query = parse_qs(raw_query)
        locale = normalize_report_locale(query.get("locale", ["en-US"])[0])
        limit = parse_int(query.get("limit", ["5000"])[0], 5000)
        start = parse_float(query.get("start", [""])[0], 0) or None
        end = parse_float(query.get("end", [""])[0], 0) or None
        requested_cleanroom = query.get("cleanroom", [""])[0].strip() or None
        requested_room_ids = set(parse_query_values(query, "cleanroom_ids", "cleanroom_id"))
        requested_device_ids = parse_query_values(query, "device_ids", "device_id")
        requested_device_id_set = set(requested_device_ids)
        customer_id = str(self.user["customer_id"])
        configured_rooms = MONITOR.configuration(customer_id) if MONITOR else [
            {"id": name, "name": name} for name in list_cleanrooms(customer_id)
        ]
        if requested_room_ids:
            configured_rooms = [room for room in configured_rooms if str(room["id"]) in requested_room_ids]
        elif requested_cleanroom:
            configured_rooms = [room for room in configured_rooms if room["name"] == requested_cleanroom]
        if requested_device_id_set:
            configured_rooms = [
                room for room in configured_rooms
                if any(str(device["id"]) in requested_device_id_set for device in room.get("devices", []))
            ]
        cleanroom_rows: dict[str, list[dict[str, object]]] = {}
        for room in configured_rooms:
            cleanroom_rows[str(room["name"])] = list_readings(
                limit,
                start=start,
                end=end,
                customer_id=customer_id,
                cleanroom_ids=[str(room["id"])],
                device_ids=requested_device_ids,
            )
        body = workbook_xlsx(cleanroom_rows, locale)
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.send_header(
            "Content-Disposition", f"attachment; filename=hawkhive-cleanroom-report-{locale}.xlsx",
        )
        self.send_header("Content-Language", locale)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve_static(self, path: str) -> None:
        rel = "collector.html" if path in ("", "/") else path.lstrip("/")
        target = (PUBLIC / rel).resolve()
        if PUBLIC not in target.parents and target != PUBLIC:
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def write_json(
        self,
        payload: object,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def request_shutdown() -> None:
    """Request a clean stop from a service control thread or test harness."""
    server = HTTP_SERVER
    if server is not None:
        server.shutdown()


def run_collector(bind: str = "127.0.0.1", port: int = 8787) -> int:
    global POWER_GUARD
    # Service, managed EXE and portable entry points all use this lifetime.
    with SystemSleepInhibitor() as guard:
        POWER_GUARD = guard
        try:
            return _run_collector(bind, port)
        finally:
            POWER_GUARD = None


def _run_collector(bind: str, port: int) -> int:
    global MONITOR, AUTH, CLOUD_SYNC, HTTP_SERVER, STORAGE
    machine_lock = MachineInstanceLock()
    machine_lock.acquire()
    instance_lock = SingleInstanceLock(RUNTIME_PATHS.lock_file)
    server: ThreadingHTTPServer | None = None
    try:
        instance_lock.acquire()
        RUNTIME_PATHS.ensure()
        migrated_files = migrate_legacy_runtime_data(RUNTIME_PATHS) if os.name == "nt" else []
        if migrated_files:
            print(f"Migrated legacy runtime data: {', '.join(migrated_files)}")
        init_db()
        AUTH = AuthService(DB_PATH)
        initial_credentials = AUTH.init_schema()
        MONITOR = MonitoringService(DB_PATH, demo_reading, record_reading, add_log)
        MONITOR.init_schema()
        STORAGE = LocalStorageManager(DB_PATH, RUNTIME_PATHS.backups_dir, add_log)
        saved_settings = load_settings(COLLECTOR_SETTINGS_PATH)
        if saved_settings.configured:
            MONITOR.options.demo = False
        CLOUD_SYNC = CloudSyncService(
            DB_PATH, saved_settings.cloud_url, saved_settings.token, saved_settings.site_id, add_log
        ) if saved_settings.configured else None
        if CLOUD_SYNC:
            CLOUD_SYNC.latest_provider = MONITOR.latest_all
            CLOUD_SYNC.config_applier = MONITOR.apply_edge_configuration
            CLOUD_SYNC.status_provider = collector_runtime_snapshot

        # Bind before starting worker threads so a port conflict cannot leave orphan workers behind.
        server = ThreadingHTTPServer((bind, port), DashboardHandler)
        HTTP_SERVER = server
        MONITOR.start()
        STORAGE.start()
        if CLOUD_SYNC:
            CLOUD_SYNC.start()
        if POWER_GUARD and POWER_GUARD.state != "unsupported":
            add_log("INFO" if POWER_GUARD.state == "active" else "WARNING",
                    "sleep_prevention", json.dumps(POWER_GUARD.status()))
        print(f"HawkHive Cleanroom Monitoring: http://{bind}:{port}")
        print(f"Runtime data: {RUNTIME_PATHS.data_dir}")
        configured_devices = [
            (room, device)
            for room in MONITOR.configuration(active_collector_customer_id())
            for device in room.get("devices", [])
            if device.get("enabled", True)
        ]
        if configured_devices:
            print(f"Configured devices: {len(configured_devices)}")
            for room, device in configured_devices:
                print(
                    f"  {room['name']} / {device['name']}: "
                    f"{device['host']}:{device['tcpPort']}, slave={device['slave']}"
                )
        else:
            print("Configured devices: 0")
        if initial_credentials:
            print("Initial customer login (shown once):")
            print(f"  Username: {initial_credentials[0]}")
            print(f"  Password: {initial_credentials[1]}")
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        HTTP_SERVER = None
        if CLOUD_SYNC:
            CLOUD_SYNC.stop()
        if MONITOR:
            MONITOR.stop()
        if STORAGE:
            STORAGE.stop()
        if server:
            server.server_close()
        instance_lock.release()
        machine_lock.release()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the HawkHive cleanroom monitor.")
    parser.add_argument("--bind", default="127.0.0.1", help="Bind address. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8787, help="Dashboard port. Default: 8787")
    args = parser.parse_args()
    try:
        return run_collector(args.bind, args.port)
    except AlreadyRunningError as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
