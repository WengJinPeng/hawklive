from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
import tempfile
from contextlib import closing
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from single_instance import AlreadyRunningError, SingleInstanceLock

DEFAULT_RETENTION_DAYS = 90
DEFAULT_BACKUP_RETENTION_DAYS = 7
DEFAULT_MAINTENANCE_SECONDS = 60 * 60


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(os.environ.get(name, default)), maximum))
    except ValueError:
        return default


def database_size_bytes(path: Path) -> int:
    return sum(
        candidate.stat().st_size
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )


class LocalStorageManager:
    """Back up SQLite and prune only records already acknowledged by the cloud."""

    def __init__(
        self,
        database: Path,
        backup_dir: Path,
        add_log: Callable[[str, str, str], None] | None = None,
        *,
        interval_seconds: int = DEFAULT_MAINTENANCE_SECONDS,
        retention_days: int | None = None,
        backup_retention_days: int = DEFAULT_BACKUP_RETENTION_DAYS,
    ) -> None:
        self.database = database
        self.backup_dir = backup_dir
        self.add_log = add_log
        self.interval_seconds = max(60, interval_seconds)
        self.retention_days = retention_days or _bounded_env_int(
            "DCP_LOCAL_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, 7, 3650
        )
        self.backup_retention_days = max(2, min(backup_retention_days, 365))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_run_at: float | None = None
        self._last_backup_at: float | None = None
        self._last_error: str | None = None
        self._integrity = "not_checked"
        self._last_deleted = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.run_once()
        self._thread = threading.Thread(
            target=self._loop, name="local-storage-maintenance", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self.run_once()

    def _backup_path(self, now: float) -> Path:
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        return self.backup_dir / f"local-{day}.sqlite3"

    def _create_daily_backup(self, now: float) -> bool:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        target = self._backup_path(now)
        if target.exists():
            self._last_backup_at = target.stat().st_mtime
            return False
        # Each attempt owns its temporary file. Never unlink another process's
        # file (including a leftover from an older collector).
        fd, name = tempfile.mkstemp(prefix=f".{target.stem}-", suffix=".tmp", dir=self.backup_dir)
        os.close(fd)
        temporary = Path(name)
        try:
            with (
                closing(sqlite3.connect(self.database, timeout=10)) as source,
                closing(sqlite3.connect(temporary)) as destination,
            ):
                source.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("Backup integrity check failed")
            # A sqlite connection context only commits/rolls back; closing()
            # above releases Windows handles before replacing the file.
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        self._last_backup_at = target.stat().st_mtime
        return True

    def _prune_backups(self, now: float) -> int:
        cutoff = now - self.backup_retention_days * 86400
        deleted = 0
        for path in self.backup_dir.glob("local-????-??-??.sqlite3"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        return deleted

    def _prune_database(self, now: float) -> int:
        cutoff = now - self.retention_days * 86400
        deleted = 0
        with closing(sqlite3.connect(self.database, timeout=10)) as db, db:
            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "readings" in tables:
                columns = {row[1] for row in db.execute("PRAGMA table_info(readings)")}
                if {"timestamp", "synced_at"}.issubset(columns):
                    cursor = db.execute(
                        "DELETE FROM readings WHERE timestamp < ? AND synced_at IS NOT NULL",
                        (cutoff,),
                    )
                    deleted += max(0, cursor.rowcount)
            if "alarm_events" in tables:
                columns = {row[1] for row in db.execute("PRAGMA table_info(alarm_events)")}
                if {"started_at", "ended_at", "synced_at"}.issubset(columns):
                    cursor = db.execute(
                        """DELETE FROM alarm_events
                           WHERE started_at < ? AND ended_at IS NOT NULL AND synced_at IS NOT NULL""",
                        (cutoff,),
                    )
                    deleted += max(0, cursor.rowcount)
            if "logs" in tables:
                log_cutoff = now - min(self.retention_days, 30) * 86400
                cursor = db.execute("DELETE FROM logs WHERE timestamp < ?", (log_cutoff,))
                deleted += max(0, cursor.rowcount)
            integrity_row = db.execute("PRAGMA quick_check").fetchone()
            self._integrity = str(integrity_row[0]) if integrity_row else "unknown"
            # A checkpoint cannot run while this connection still owns the
            # write transaction opened by the DELETE statements above.
            db.commit()
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return deleted

    def run_once(self, now: float | None = None) -> None:
        # Also serialize maintenance invoked by different manager objects or
        # processes. Contention is not a storage failure and must not trigger
        # cleanup of another attempt's temporary files.
        maintenance_lock = SingleInstanceLock(self.backup_dir / "maintenance.lock")
        try:
            maintenance_lock.acquire()
        except AlreadyRunningError:
            return
        except OSError as exc:
            with self._lock:
                self._last_run_at = time.time() if now is None else now
                self._last_error = str(exc)
            if self.add_log:
                self.add_log("ERROR", "local_storage_maintenance_failed", str(exc))
            return
        try:
            self._run_once_locked(now)
        finally:
            maintenance_lock.release()

    def _run_once_locked(self, now: float | None = None) -> None:
        current = time.time() if now is None else now
        try:
            backup_created = self._create_daily_backup(current)
            deleted = self._prune_database(current)
            backups_deleted = self._prune_backups(current)
            with self._lock:
                self._last_run_at = current
                self._last_error = None
                self._last_deleted = deleted
            if self.add_log and (backup_created or deleted or backups_deleted):
                self.add_log(
                    "INFO", "local_storage_maintained",
                    f"backup_created={backup_created}; rows_deleted={deleted}; backups_deleted={backups_deleted}",
                )
        except (OSError, sqlite3.Error) as exc:
            with self._lock:
                self._last_run_at = current
                self._last_error = str(exc)
            if self.add_log:
                self.add_log("ERROR", "local_storage_maintenance_failed", str(exc))

    def status(self) -> dict[str, object]:
        directory = self.database.parent
        usage = shutil.disk_usage(directory)
        free_ratio = usage.free / usage.total if usage.total else 0.0
        with self._lock:
            if usage.free < 1024**3 or (free_ratio < 0.03 and usage.free < 5 * 1024**3):
                state = "critical"
            elif usage.free < 10 * 1024**3 or (free_ratio < 0.10 and usage.free < 20 * 1024**3):
                state = "warning"
            elif self._last_error or self._integrity != "ok":
                state = "attention"
            else:
                state = "ok"
            return {
                "state": state,
                "data_dir": str(directory),
                "database_bytes": database_size_bytes(self.database),
                "disk_total_bytes": usage.total,
                "disk_free_bytes": usage.free,
                "disk_free_percent": round(free_ratio * 100, 1),
                "retention_days": self.retention_days,
                "unsynced_records_protected": True,
                "integrity": self._integrity,
                "last_run_at": self._last_run_at,
                "last_backup_at": self._last_backup_at,
                "last_error": self._last_error,
                "last_deleted": self._last_deleted,
            }
