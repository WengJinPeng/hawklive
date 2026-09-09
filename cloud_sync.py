from __future__ import annotations

import json
import os
import platform
import random
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from device_discovery import scan_modbus_networks
from collector_diagnostics import init_queue, enqueue, prune_queue, redact, diagnostic_scope

COLLECTOR_VERSION = "0.6.1"


def canonical_uuid(value: object) -> str:
    """Return one comparison form for compact and hyphenated UUID strings."""
    return UUID(str(value)).hex


class CloudSyncService:
    """Uploads unsynced SQLite readings in idempotent HTTPS batches."""

    def __init__(
        self,
        db_path: Path,
        base_url: str,
        token: str,
        site_id: str,
        add_log: Callable[[str, str, str], None],
        batch_size: int = 200,
        latest_provider: Callable[[], list[dict[str, object]]] | None = None,
        config_applier: Callable[[dict[str, object]], None] | None = None,
        config_interval: float = 30.0,
        status_provider: Callable[[], dict[str, object]] | None = None,
        heartbeat_interval: float = 30.0,
        discovery_scanner: Callable[[str | None, int, str], tuple[list[str], list[dict[str, object]]]] | None = None,
        discovery_interval: float = 10.0,
        remote_discovery_enabled: bool | None = None,
        max_item_attempts: int = 5,
    ) -> None:
        self.db_path = db_path
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.site_id = site_id
        self.customer_id: str | None = None
        def scoped_log(level, event, message):
            with diagnostic_scope(self.customer_id, self.site_id):
                add_log(level, event, redact(message, (self.token,)))
        self.add_log = scoped_log
        self.batch_size = max(1, min(batch_size, 1000))
        self.latest_provider = latest_provider
        self.config_applier = config_applier
        self.config_interval = max(10.0, config_interval)
        self.status_provider = status_provider
        self.heartbeat_interval = max(10.0, heartbeat_interval)
        self._assigned_hosts: set[str] = set()
        self.discovery_scanner = discovery_scanner or (
            lambda requested_cidr, tcp_port, base_url: scan_modbus_networks(
                requested_cidr, tcp_port, base_url,
                excluded_hosts=set(self._assigned_hosts),
            )
        )
        self.discovery_interval = max(5.0, discovery_interval)
        self.remote_discovery_enabled = (
            os.environ.get("DCP_REMOTE_DISCOVERY_ENABLED", "1").strip().casefold()
            in {"1", "true", "yes", "on"}
            if remote_discovery_enabled is None else remote_discovery_enabled
        )
        self.max_item_attempts = max(1, max_item_attempts)
        self.instance_id = self._load_or_create_instance_id()
        self._last_config_check = 0.0
        self._last_heartbeat_check = 0.0
        self._last_discovery_check = 0.0
        self._last_config_revision = self._load_applied_config_revision()
        self.desired_config_revision: int | None = None
        self.config_apply_status = "applied" if self._last_config_revision is not None else "awaiting"
        self.config_apply_error: str | None = None
        self.site_name: str | None = None
        self.last_heartbeat_at: float | None = None
        self.last_success_at: float | None = None
        self.last_attempt_at: float | None = None
        self.last_contact_at: float | None = None
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.consecutive_failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status_lock = threading.RLock()
        self._channel_context = threading.local()
        self._channel_errors: dict[str, str] = {}
        self._config_ready = threading.Event()
        self._last_diagnostic_snapshot = 0.0
        with self.connect() as db:
            init_queue(db)

    @classmethod
    def from_environment(
        cls, db_path: Path, add_log: Callable[[str, str, str], None]
    ) -> CloudSyncService | None:
        url = os.environ.get("DCP_CLOUD_URL", "").strip()
        token = os.environ.get("DCP_CLOUD_TOKEN", "").strip()
        site_id = os.environ.get("DCP_SITE_ID", "site-001").strip()
        if not url or not token:
            return None
        parsed = urllib.parse.urlparse(url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not local_http and os.environ.get("DCP_CLOUD_ALLOW_HTTP") != "1":
            raise ValueError("DCP_CLOUD_URL must use HTTPS")
        return cls(db_path, url, token, site_id, add_log)

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

    def _load_or_create_instance_id(self) -> str:
        """Persist a non-secret node identity so copied tokens cannot create double collection."""
        with self.connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS cloud_sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            row = db.execute(
                "SELECT value FROM cloud_sync_state WHERE key='collector_instance_id'"
            ).fetchone()
            if row and str(row[0]).strip():
                return str(row[0]).strip()
            instance_id = f"collector:{uuid4().hex}"
            db.execute(
                "INSERT INTO cloud_sync_state(key,value) VALUES('collector_instance_id',?)",
                (instance_id,),
            )
            return instance_id

    def _load_applied_config_revision(self) -> int | None:
        key = f"applied_config_revision:{self.site_id}"
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM cloud_sync_state WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            return max(0, int(row[0]))
        except (TypeError, ValueError):
            return None

    def _save_applied_config_revision(self, revision: int) -> None:
        key = f"applied_config_revision:{self.site_id}"
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO cloud_sync_state(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, str(revision)),
            )

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": f"DCP8001-Edge/{COLLECTOR_VERSION}",
            "X-DCP-Collector-Instance": self.instance_id,
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _request_json(self, request: urllib.request.Request) -> dict[str, object]:
        self.last_attempt_at = time.time()
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError:
            self.last_contact_at = time.time()
            raise
        self.last_contact_at = time.time()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("Cloud returned a non-object JSON response")
        return payload

    def _mark_success(self) -> None:
        with self._status_lock:
            self.last_success_at = time.time()
            self._channel_errors.pop(getattr(self._channel_context, "name", "connection"), None)
            self.last_error = "; ".join(f"{key}: {value}" for key, value in self._channel_errors.items())[:500] or None
            if not self._channel_errors:
                self.consecutive_failures = 0

    def _mark_failure(self, exc: Exception) -> None:
        with self._status_lock:
            name = getattr(self._channel_context, "name", "connection")
            self._channel_errors[name] = str(exc)[:500] or exc.__class__.__name__
            self.last_error = "; ".join(f"{key}: {value}" for key, value in self._channel_errors.items())[:500]
            self.last_error_at = time.time()
            self.consecutive_failures += 1

    def quarantined_count(self) -> int:
        with self.connect() as db:
            customer_clause = " AND customer_id = ?" if self.customer_id else ""
            parameters: tuple[object, ...] = (
                (self.site_id, self.customer_id)
                if self.customer_id else (self.site_id,)
            )
            readings = int(db.execute(
                f"""SELECT COUNT(*) FROM readings
                    WHERE site_id = ? AND source = 'device'
                      AND sync_quarantined_at IS NOT NULL{customer_clause}""",
                parameters,
            ).fetchone()[0])
            alarms = int(db.execute(
                f"""SELECT COUNT(*) FROM alarm_events
                    WHERE site_id = ? AND source = 'device'
                      AND sync_quarantined_at IS NOT NULL{customer_clause}""",
                parameters,
            ).fetchone()[0])
        return readings + alarms

    def unassigned_count(self) -> int:
        with self.connect() as db:
            readings = int(db.execute(
                """SELECT COUNT(*) FROM readings
                   WHERE source = 'device' AND synced_at IS NULL
                     AND sync_quarantined_at IS NULL
                     AND COALESCE(site_id, '') <> ?
                     AND cleanroom_id <> '' AND device_id <> ''""",
                (self.site_id,),
            ).fetchone()[0])
            alarms = int(db.execute(
                """SELECT COUNT(*) FROM alarm_events
                   WHERE source = 'device' AND synced_at IS NULL
                     AND sync_quarantined_at IS NULL AND COALESCE(site_id, '') <> ?""",
                (self.site_id,),
            ).fetchone()[0])
        return readings + alarms

    def oldest_pending_at(self) -> float | None:
        with self.connect() as db:
            if self.customer_id:
                row = db.execute(
                    """SELECT MIN(timestamp) FROM readings
                       WHERE customer_id = ? AND site_id = ? AND source = 'device'
                         AND synced_at IS NULL AND sync_quarantined_at IS NULL
                         AND cleanroom_id <> '' AND device_id <> ''""",
                    (self.customer_id, self.site_id),
                ).fetchone()
            else:
                row = db.execute(
                    """SELECT MIN(timestamp) FROM readings
                       WHERE site_id = ? AND source = 'device'
                         AND synced_at IS NULL AND sync_quarantined_at IS NULL
                         AND cleanroom_id <> '' AND device_id <> ''""",
                    (self.site_id,),
                ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def pending_count(self) -> int:
        with self.connect() as db:
            if not self.customer_id:
                return int(db.execute(
                    """SELECT COUNT(*) FROM readings WHERE synced_at IS NULL
                       AND site_id = ? AND source = 'device'
                       AND sync_quarantined_at IS NULL
                       AND cleanroom_id <> '' AND device_id <> ''""",
                    (self.site_id,),
                ).fetchone()[0])
            return int(db.execute(
                """SELECT COUNT(*) FROM readings
                   WHERE customer_id = ? AND synced_at IS NULL
                     AND site_id = ? AND source = 'device'
                     AND sync_quarantined_at IS NULL
                     AND cleanroom_id <> '' AND device_id <> ''""",
                (self.customer_id, self.site_id),
            ).fetchone()[0])

    def _apply_configuration_payload(self, data: dict[str, object], revision: int) -> bool:
        self.desired_config_revision = revision
        self.site_name = str(data.get("site_name", "")).strip() or self.site_name
        rooms = data.get("rooms")
        assigned_hosts: set[str] = set()
        if isinstance(rooms, list):
            for room in rooms:
                if not isinstance(room, dict) or not isinstance(room.get("devices"), list):
                    continue
                for device in room["devices"]:
                    if not isinstance(device, dict) or device.get("enabled", True) is False:
                        continue
                    host = str(device.get("host", "")).strip()
                    if host:
                        assigned_hosts.add(host)
        # Replace atomically so the independent discovery worker always sees a
        # complete snapshot, including when the revision was already applied.
        self._assigned_hosts = assigned_hosts
        if revision == self._last_config_revision:
            self.config_apply_status = "applied"
            self.config_apply_error = None
            return False
        if self.config_applier is None:
            self.config_apply_status = "awaiting"
            return False
        try:
            self.config_applier(data)
        except Exception as exc:
            self.config_apply_status = "failed"
            self.config_apply_error = str(exc)[:500] or exc.__class__.__name__
            raise
        self._last_config_revision = revision
        self._save_applied_config_revision(revision)
        self.config_apply_status = "applied"
        self.config_apply_error = None
        return True

    def pull_config_once(self) -> int:
        if self.config_applier is None:
            return 0
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/edge/config",
            headers=self._headers(),
        )
        payload = self._request_json(request)
        data = payload.get("data")
        if not payload.get("ok") or not isinstance(data, dict):
            raise RuntimeError("Cloud returned an invalid edge configuration")
        if str(data.get("site_id", "")) != self.site_id:
            raise RuntimeError("Cloud returned a configuration for another site")
        customer_id = str(data.get("customer_id", "")).strip()
        if not customer_id:
            raise RuntimeError("Cloud configuration is missing its customer id")
        self.customer_id = customer_id
        revision = int(data.get("revision", 0))
        changed = self._apply_configuration_payload(data, revision)
        self._config_ready.set()
        self._mark_success()
        if not changed:
            return 0
        self.add_log("INFO", "cloud_config_applied", f"Applied cloud configuration revision {revision}")
        return revision

    def heartbeat_once(self) -> int:
        context: dict[str, object] = {}
        if self.status_provider:
            try:
                supplied = self.status_provider()
                if isinstance(supplied, dict):
                    context = supplied
            except Exception as exc:
                context = {"last_error": f"Local status unavailable: {exc}"[:500]}
        storage = context.get("storage")
        storage_status = storage if isinstance(storage, dict) else {}
        latest_error = context.get("last_error") or self.last_error
        body = json.dumps(
            {
                "site_id": self.site_id,
                "collector_time": time.time(),
                "version": COLLECTOR_VERSION,
                "hostname": socket.gethostname()[:200] or "unknown",
                "platform": platform.platform()[:200] or platform.system() or "unknown",
                "monitor_running": bool(context.get("monitor_running", True)),
                "device_total": int(context.get("device_total") or 0),
                "device_online": int(context.get("device_online") or 0),
                "device_states": context.get("device_states"),
                "update_status": context.get("update_status"),
                "last_reading_at": context.get("last_reading_at"),
                "pending_uploads": self.pending_count(),
                "quarantined_uploads": self.quarantined_count(),
                "unassigned_uploads": self.unassigned_count(),
                "oldest_pending_at": self.oldest_pending_at(),
                "storage_state": str(storage_status.get("state", "unknown"))[:40],
                "database_bytes": storage_status.get("database_bytes"),
                "disk_free_bytes": storage_status.get("disk_free_bytes"),
                "applied_config_version": self._last_config_revision,
                "config_apply_status": self.config_apply_status,
                "config_apply_error": self.config_apply_error,
                "last_error": str(latest_error)[:500] if latest_error else None,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/edge/heartbeat",
            data=body,
            method="POST",
            headers=self._headers(json_body=True),
        )
        payload = self._request_json(request)
        data = payload.get("data")
        if not payload.get("ok") or not isinstance(data, dict):
            raise RuntimeError("Cloud rejected the collector heartbeat")
        desired_revision = int(data.get("desired_config_version", 0))
        self.desired_config_revision = desired_revision
        self.last_heartbeat_at = time.time()
        self._mark_success()
        return desired_revision

    def push_config_once(self, payload: dict[str, object]) -> list[dict[str, object]]:
        if not self.customer_id:
            self.pull_config_once()
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/edge/config",
            data=body,
            method="POST",
            headers=self._headers(json_body=True),
        )
        response_payload = self._request_json(request)
        rooms = response_payload.get("data")
        if not response_payload.get("ok") or not isinstance(rooms, list):
            raise RuntimeError("Cloud rejected the collector configuration")
        self._mark_success()
        if self.config_applier:
            self.config_applier({
                "customer_id": self.customer_id,
                "site_id": self.site_id,
                "revision": int(time.time()),
                "rooms": rooms,
            })
        self.add_log("INFO", "cloud_config_saved", "Device configuration saved to cloud")
        return rooms

    def _create_topology_item_once(
        self, path: str, payload: dict[str, object], event: str
    ) -> list[dict[str, object]]:
        """Create a cloud-owned topology item, then atomically apply the returned revision."""
        if not self.customer_id:
            self.pull_config_once()
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method="POST",
            headers=self._headers(json_body=True),
        )
        response_payload = self._request_json(request)
        data = response_payload.get("data")
        if not response_payload.get("ok") or not isinstance(data, dict):
            raise RuntimeError("Cloud rejected the topology change")
        if str(data.get("customer_id", "")) != str(self.customer_id):
            raise RuntimeError("Cloud returned topology for another customer")
        if str(data.get("site_id", "")) != self.site_id:
            raise RuntimeError("Cloud returned topology for another site")
        rooms = data.get("rooms")
        if not isinstance(rooms, list):
            raise RuntimeError("Cloud returned an invalid topology configuration")
        revision = int(data.get("revision", 0))
        self._apply_configuration_payload(data, revision)
        self._mark_success()
        self.add_log("INFO", event, f"Applied cloud configuration revision {revision}")
        return rooms

    def create_cleanroom_once(self, name: str) -> list[dict[str, object]]:
        return self._create_topology_item_once(
            "/api/v1/edge/cleanrooms", {"name": name}, "cloud_cleanroom_created"
        )

    def create_device_once(self, payload: dict[str, object]) -> list[dict[str, object]]:
        return self._create_topology_item_once(
            "/api/v1/edge/devices", payload, "cloud_device_created"
        )

    def pull_discovery_job_once(self) -> int:
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/edge/discovery/jobs",
            headers=self._headers(),
        )
        try:
            payload = self._request_json(request)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self._mark_success()
                return 0
            raise
        if not payload.get("ok"):
            raise RuntimeError("Cloud rejected the discovery check")
        job = payload.get("data")
        if job is None:
            self._mark_success()
            return 0
        if not isinstance(job, dict) or str(job.get("site_id", "")) != self.site_id:
            raise RuntimeError("Cloud returned an invalid discovery job")
        job_id = str(job.get("id", "")).strip()
        if not job_id:
            raise RuntimeError("Cloud discovery job is missing its id")
        try:
            scanned_cidrs, results = self.discovery_scanner(
                str(job["requested_cidr"]) if job.get("requested_cidr") else None,
                int(job.get("tcp_port", 502)),
                self.base_url,
            )
            completion: dict[str, object] = {
                "status": "completed",
                "scanned_cidr": scanned_cidrs[0] if scanned_cidrs else None,
                "scanned_cidrs": scanned_cidrs,
                "results": results, "error": None,
            }
        except Exception as exc:
            completion = {
                "status": "failed", "scanned_cidr": None,
                "scanned_cidrs": [], "results": [],
                "error": str(exc)[:500] or "Device discovery failed",
            }
        body = json.dumps(completion, separators=(",", ":")).encode("utf-8")
        report = urllib.request.Request(
            f"{self.base_url}/api/v1/edge/discovery/jobs/{urllib.parse.quote(job_id)}/result",
            data=body,
            method="POST",
            headers=self._headers(json_body=True),
        )
        acknowledged = self._request_json(report)
        if not acknowledged.get("ok"):
            raise RuntimeError("Cloud did not acknowledge the discovery result")
        self._mark_success()
        if completion["status"] == "completed":
            self.add_log(
                "INFO", "device_discovery_completed",
                f"Scanned {', '.join(completion['scanned_cidrs'])}; found {len(completion['results'])} candidate(s)",
            )
        else:
            self.add_log("ERROR", "device_discovery_failed", str(completion["error"]))
        return 1

    def _pending_rows(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            if not self.customer_id:
                return db.execute(
                    """SELECT * FROM readings WHERE synced_at IS NULL
                       AND site_id = ? AND source = 'device'
                       AND sync_quarantined_at IS NULL
                       AND cleanroom_id <> '' AND device_id <> ''
                       ORDER BY sync_attempts, timestamp, id LIMIT ?""",
                    (self.site_id, self.batch_size),
                ).fetchall()
            return db.execute(
                """
                SELECT * FROM readings
                WHERE customer_id = ? AND synced_at IS NULL
                  AND site_id = ? AND source = 'device'
                  AND sync_quarantined_at IS NULL
                  AND cleanroom_id <> '' AND device_id <> ''
                ORDER BY sync_attempts, timestamp, id LIMIT ?
                """,
                (self.customer_id, self.site_id, self.batch_size),
            ).fetchall()

    def _payload(self, rows: list[sqlite3.Row]) -> dict[str, object]:
        readings = []
        for row in rows:
            keys = set(row.keys())
            readings.append(
                {
                    "record_uuid": row["record_uuid"],
                    "customer_id": row["customer_id"],
                    "site_id": row["site_id"],
                    "cleanroom_id": row["cleanroom_id"],
                    "cleanroom_name": row["cleanroom"],
                    "device_id": row["device_id"],
                    "device_name": row["device"],
                    "measured_at": row["timestamp"],
                    "source": row["source"],
                    "particles": json.loads(row["particles_json"]),
                    "environment": json.loads(row["environment_json"]),
                    "alarm_status": row["alarm_status"],
                    "alarm_details": json.loads(row["alarm_details_json"]),
                    "cleanliness_code": row["cleanliness_code"],
                    "cleanliness_label": row["cleanliness_label"],
                    "particle_unit_code": row["particle_unit_code"] if "particle_unit_code" in keys else None,
                    "particle_unit_label": row["particle_unit_label"] if "particle_unit_label" in keys else None,
                    "protocol_profile": row["protocol_profile"] if "protocol_profile" in keys else None,
                }
            )
        return {"site_id": self.site_id, "readings": readings}

    @staticmethod
    def _permanent_item_error(exc: Exception) -> bool:
        return isinstance(exc, urllib.error.HTTPError) and exc.code in {400, 422}

    def _record_queue_failure(
        self,
        table: str,
        key_column: str,
        identifiers: list[str],
        exc: Exception,
        permanent: bool,
    ) -> int:
        if not identifiers:
            return 0
        if (table, key_column) not in {("readings", "record_uuid"), ("alarm_events", "id")}:
            raise ValueError("Unsupported upload queue")
        placeholders = ",".join("?" for _ in identifiers)
        error = str(exc)[:500] or exc.__class__.__name__
        quarantined_at = time.time()
        with self.connect() as db:
            if permanent:
                db.execute(
                    f"""
                    UPDATE {table}
                    SET sync_attempts = sync_attempts + 1,
                        sync_error = ?,
                        sync_quarantined_at = CASE
                            WHEN sync_attempts + 1 >= ?
                            THEN COALESCE(sync_quarantined_at, ?)
                            ELSE sync_quarantined_at
                        END
                    WHERE {key_column} IN ({placeholders})
                    """,
                    (error, self.max_item_attempts, quarantined_at, *identifiers),
                )
                quarantined = int(db.execute(
                    f"""SELECT COUNT(*) FROM {table}
                        WHERE {key_column} IN ({placeholders})
                          AND sync_quarantined_at = ?""",
                    (*identifiers, quarantined_at),
                ).fetchone()[0])
            else:
                db.execute(
                    f"UPDATE {table} SET sync_error = ? WHERE {key_column} IN ({placeholders})",
                    (error, *identifiers),
                )
                quarantined = 0
        if quarantined:
            self.add_log(
                "ERROR",
                "cloud_item_quarantined",
                f"Quarantined {quarantined} invalid {table} item(s) after "
                f"{self.max_item_attempts} rejected uploads",
            )
        return quarantined

    def _upload_reading_rows(self, rows: list[sqlite3.Row]) -> int:
        body = json.dumps(self._payload(rows), separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/readings/batch",
            data=body,
            method="POST",
            headers=self._headers(json_body=True),
        )
        payload = self._request_json(request)
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("error") or "Cloud rejected batch"))
        accepted = {canonical_uuid(value) for value in payload.get("accepted", [])}
        sent = [str(row["record_uuid"]) for row in rows]
        if accepted != {canonical_uuid(value) for value in sent}:
            raise RuntimeError("Cloud did not acknowledge every record")
        placeholders = ",".join("?" for _ in sent)
        with self.connect() as db:
            db.execute(
                f"""UPDATE readings
                    SET synced_at = ?, sync_error = NULL, sync_quarantined_at = NULL
                    WHERE record_uuid IN ({placeholders})""",
                (time.time(), *sent),
            )
        self._mark_success()
        return len(sent)

    def sync_once(self) -> int:
        rows = self._pending_rows()
        if not rows:
            return 0
        try:
            return self._upload_reading_rows(rows)
        except urllib.error.HTTPError as exc:
            if not self._permanent_item_error(exc):
                self._record_queue_failure(
                    "readings", "record_uuid", [str(row["record_uuid"]) for row in rows], exc, False,
                )
                raise
            if len(rows) == 1:
                self._record_queue_failure(
                    "readings", "record_uuid", [str(rows[0]["record_uuid"])], exc, True,
                )
                return 0
            sent = 0
            for row in rows:
                try:
                    sent += self._upload_reading_rows([row])
                except urllib.error.HTTPError as item_exc:
                    if not self._permanent_item_error(item_exc):
                        self._record_queue_failure(
                            "readings", "record_uuid", [str(row["record_uuid"])], item_exc, False,
                        )
                        raise
                    self._record_queue_failure(
                        "readings", "record_uuid", [str(row["record_uuid"])], item_exc, True,
                    )
            return sent
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            self._record_queue_failure(
                "readings", "record_uuid", [str(row["record_uuid"]) for row in rows], exc, False,
            )
            raise

    def sync_latest_once(self) -> int:
        if self.latest_provider is None:
            return 0
        values = [
            item for item in self.latest_provider()
            if item.get("particles") and item.get("online") is not False
            and str(item.get("source")) == "device"
            and str(item.get("site_id", "")) == self.site_id
            and (not self.customer_id or str(item.get("customer_id")) == self.customer_id)
        ]
        if not values:
            return 0
        readings = []
        for item in values:
            readings.append(
                {
                    "customer_id": item["customer_id"], "site_id": self.site_id,
                    "cleanroom_id": item["cleanroom_id"], "cleanroom_name": item["cleanroom"],
                    "device_id": item["device_id"], "device_name": item["device"],
                    "measured_at": item["timestamp"], "source": item["source"],
                    "particles": item["particles"], "environment": item["environment"],
                    "alarm_status": item["alarm_status"], "alarm_details": item["alarm_details"],
                    "cleanliness_code": item["cleanliness_code"],
                    "cleanliness_label": item["cleanliness_label"],
                    "particle_unit_code": item.get("particle_unit_code"),
                    "particle_unit_label": item.get("particle_unit_label"),
                    "protocol_profile": item.get("protocol_profile"),
                }
            )
        body = json.dumps({"site_id": self.site_id, "readings": readings}, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/latest/batch", data=body, method="POST",
            headers=self._headers(json_body=True),
        )
        payload = self._request_json(request)
        if not payload.get("ok") or int(payload.get("accepted", -1)) != len(readings):
            raise RuntimeError("Cloud did not acknowledge latest readings")
        self._mark_success()
        return len(readings)

    def sync_alarms_once(self) -> int:
        with self.connect() as db:
            if self.customer_id:
                rows = db.execute(
                    """SELECT * FROM alarm_events WHERE customer_id = ? AND synced_at IS NULL
                       AND site_id = ? AND source = 'device'
                       AND sync_quarantined_at IS NULL
                       ORDER BY sync_attempts, started_at LIMIT ?""",
                    (self.customer_id, self.site_id, self.batch_size),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT * FROM alarm_events WHERE synced_at IS NULL
                       AND site_id = ? AND source = 'device'
                       AND sync_quarantined_at IS NULL
                       ORDER BY sync_attempts, started_at LIMIT ?""",
                    (self.site_id, self.batch_size),
                ).fetchall()
        if not rows:
            return 0
        try:
            return self._upload_alarm_rows(rows)
        except urllib.error.HTTPError as exc:
            if not self._permanent_item_error(exc):
                self._record_queue_failure(
                    "alarm_events", "id", [str(row["id"]) for row in rows], exc, False,
                )
                raise
            if len(rows) == 1:
                self._record_queue_failure(
                    "alarm_events", "id", [str(rows[0]["id"])], exc, True,
                )
                return 0
            sent = 0
            for row in rows:
                try:
                    sent += self._upload_alarm_rows([row])
                except urllib.error.HTTPError as item_exc:
                    if not self._permanent_item_error(item_exc):
                        self._record_queue_failure(
                            "alarm_events", "id", [str(row["id"])], item_exc, False,
                        )
                        raise
                    self._record_queue_failure(
                        "alarm_events", "id", [str(row["id"])], item_exc, True,
                    )
            return sent
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            self._record_queue_failure(
                "alarm_events", "id", [str(row["id"]) for row in rows], exc, False,
            )
            raise

    def _upload_alarm_rows(self, rows: list[sqlite3.Row]) -> int:
        events = [
            {
                "event_uuid": row["id"], "customer_id": row["customer_id"],
                "site_id": row["site_id"], "cleanroom_id": row["cleanroom_id"],
                "device_id": row["device_id"], "metric": row["metric"],
                "source": row["source"],
                "started_at": row["started_at"], "ended_at": row["ended_at"],
                "trigger_value": row["trigger_value"], "peak_value": row["peak_value"],
                "limit_description": row["limit_description"],
            }
            for row in rows
        ]
        body = json.dumps({"site_id": self.site_id, "events": events}, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/alarms/batch", data=body, method="POST",
            headers=self._headers(json_body=True),
        )
        payload = self._request_json(request)
        sent = {str(row["id"]) for row in rows}
        accepted = {canonical_uuid(value) for value in payload.get("accepted", [])}
        if not payload.get("ok") or accepted != {canonical_uuid(value) for value in sent}:
            raise RuntimeError("Cloud did not acknowledge every alarm event")
        placeholders = ",".join("?" for _ in sent)
        with self.connect() as db:
            db.execute(
                f"""UPDATE alarm_events
                    SET synced_at=?, sync_error=NULL, sync_quarantined_at=NULL
                    WHERE id IN ({placeholders})""",
                (time.time(), *sent),
            )
        self._mark_success()
        return len(sent)

    def sync_diagnostics_once(self) -> int:
        if not self.customer_id:
            return 0
        now = time.monotonic()
        with self.connect() as db:
            prune_queue(db)
        if now - self._last_diagnostic_snapshot >= 300 or not self._last_diagnostic_snapshot:
            context = self.status_provider() if self.status_provider else {}
            # Allowlist only operational values; no full config, token or database.
            snapshot = {key: context.get(key) for key in (
                "monitor_running", "poll_seconds", "record_seconds", "device_total",
                "device_online", "last_reading_at",
            )}
            storage = context.get("storage") or {}
            snapshot["last_error"] = redact(context.get("last_error") or "", (self.token,))
            with self.connect() as db:
                snapshot["logs_discarded_total"] = db.execute("SELECT discarded FROM diagnostic_queue_stats WHERE id=1").fetchone()[0]
            snapshot.update(version=COLLECTOR_VERSION, config_revision=self._last_config_revision,
                            disk_free_bytes=storage.get("disk_free_bytes"), storage_state=storage.get("state"))
            with self.connect() as db:
                enqueue(db, self.customer_id, self.site_id, self.instance_id, "INFO",
                        "collector_snapshot", json.dumps(snapshot, ensure_ascii=False))
            self._last_diagnostic_snapshot = now
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM diagnostic_outbox
                WHERE customer_id=? AND site_id=? AND instance_id=? ORDER BY seq LIMIT 100""",
                (self.customer_id, self.site_id, self.instance_id)).fetchall()
        if not rows:
            return 0
        logs = [{key: row[key] for key in ("record_uuid", "timestamp", "level", "event", "message")} for row in rows]
        for item in logs:
            item["message"] = redact(item["message"], (self.token,))
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/edge/diagnostics/batch",
            data=json.dumps({"site_id": self.site_id, "logs": logs}, ensure_ascii=False).encode(),
            method="POST", headers=self._headers(json_body=True),
        )
        payload = self._request_json(request)
        expected = {canonical_uuid(item["record_uuid"]) for item in logs}
        if not payload.get("ok") or {canonical_uuid(value) for value in payload.get("accepted", [])} != expected:
            raise RuntimeError("Cloud did not acknowledge every diagnostic log")
        with self.connect() as db:
            db.executemany("DELETE FROM diagnostic_outbox WHERE record_uuid=? AND customer_id=? AND site_id=? AND instance_id=?",
                           [(item["record_uuid"], self.customer_id, self.site_id, self.instance_id) for item in logs])
        self._mark_success()
        return len(logs)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cloud-sync", daemon=True)
        self._thread.start()
        self.add_log("INFO", "cloud_sync_started", f"Cloud sync enabled for {self.site_id}")

    def connection_status(self) -> dict[str, object]:
        running = bool(self._thread and self._thread.is_alive())
        quarantined = self.quarantined_count()
        unassigned = self.unassigned_count()
        has_successful_contact = bool(
            self.last_success_at is not None
            and time.time() - self.last_success_at <= 90
        )
        if not running:
            state = "stopped"
        elif self._channel_errors and not has_successful_contact:
            state = "error"
        elif quarantined or unassigned or self._channel_errors:
            state = "attention"
        elif has_successful_contact:
            state = "connected"
        else:
            state = "connecting"
        return {
            "running": running,
            "connected": has_successful_contact,
            "state": state,
            "instance_id": self.instance_id,
            "site_name": self.site_name,
            "last_attempt_at": self.last_attempt_at,
            "last_contact_at": self.last_contact_at,
            "last_success_at": self.last_success_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "consecutive_failures": self.consecutive_failures,
            "desired_config_version": self.desired_config_revision,
            "applied_config_version": self._last_config_revision,
            "config_apply_status": self.config_apply_status,
            "config_apply_error": self.config_apply_error,
            "pending_uploads": self.pending_count(),
            "quarantined_uploads": quarantined,
            "unassigned_uploads": unassigned,
            "oldest_pending_at": self.oldest_pending_at(),
            "remote_discovery_enabled": self.remote_discovery_enabled,
            "channel_errors": dict(self._channel_errors),
        }

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=25)

    def _run_channel(self, name: str, action: Callable[[], int], interval: float, drain: bool = False) -> None:
        self._channel_context.name = name
        failure_count = 0
        while not self._stop.is_set():
            # Diagnostics must report an authenticated configuration-apply failure
            # even while measurement uploads are still waiting for configuration.
            if name not in {"config", "diagnostics"} and self.config_applier is not None and not self._config_ready.is_set():
                self._stop.wait(0.25)
                continue
            try:
                sent = action()
                failure_count = 0
                # A no-op is not proof of cloud contact. Each API action marks
                # its own successful response; clear only this channel's error.
                with self._status_lock:
                    self._channel_errors.pop(name, None)
                    self.last_error = "; ".join(f"{key}: {value}" for key, value in self._channel_errors.items())[:500] or None
                self._stop.wait(2 if drain and sent else interval)
            except Exception as exc:
                failure_count += 1
                self._mark_failure(exc)
                limit = 30.0 if name in {"latest", "heartbeat"} else 300.0
                delay = min(limit, round(2 ** min(failure_count, 8) * random.uniform(0.8, 1.2), 1))
                self.add_log("ERROR", "cloud_sync_failed", f"{name}: retry in {delay:g}s: {exc}")
                self._stop.wait(delay)

    def _run(self) -> None:
        # Independent workers prevent a slow/failing alarm or history request
        # from delaying latest readings, configuration, or the site heartbeat.
        channels = [
            ("config", self.pull_config_once, self.config_interval, False),
            ("heartbeat", self.heartbeat_once, self.heartbeat_interval, False),
            ("latest", self.sync_latest_once, 10.0, False),
            ("readings", self.sync_once, 10.0, True),
            ("alarms", self.sync_alarms_once, 10.0, True),
            ("diagnostics", self.sync_diagnostics_once, 30.0, True),
        ]
        if self.remote_discovery_enabled:
            channels.append(("discovery", self.pull_discovery_job_once, self.discovery_interval, False))
        workers = [threading.Thread(target=self._run_channel, args=args, name=f"cloud-{args[0]}", daemon=True) for args in channels]
        for worker in workers:
            worker.start()
        self._stop.wait()
        deadline = time.monotonic() + 24
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
