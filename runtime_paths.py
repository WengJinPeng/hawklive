from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

APP_DIRECTORY = Path(__file__).resolve().parent
WINDOWS_DATA_SUBDIRECTORY = Path("HawkHive") / "DCP8001"


@dataclass(frozen=True)
class RuntimePaths:
    data_dir: Path
    database: Path
    collector_settings: Path
    logs_dir: Path
    backups_dir: Path
    lock_file: Path

    def ensure(self) -> RuntimePaths:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        return self


def resolve_data_dir(
    *,
    environ: dict[str, str] | None = None,
    platform_name: str | None = None,
    app_directory: Path | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    explicit = values.get("DCP_DATA_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()

    platform = os.name if platform_name is None else platform_name
    source_dir = APP_DIRECTORY if app_directory is None else app_directory
    if platform == "nt":
        program_data = values.get("ProgramData", "").strip()
        if not program_data:
            program_data = r"C:\ProgramData"
        return Path(program_data) / WINDOWS_DATA_SUBDIRECTORY
    # Keep source checkouts and existing Linux deployments backward compatible.
    return source_dir.resolve()


def runtime_paths(**kwargs: object) -> RuntimePaths:
    data_dir = resolve_data_dir(**kwargs)
    return RuntimePaths(
        data_dir=data_dir,
        database=data_dir / "dashboard_data.sqlite3",
        collector_settings=data_dir / "collector_settings.json",
        logs_dir=data_dir / "logs",
        backups_dir=data_dir / "backups",
        lock_file=data_dir / "collector.lock",
    )


def migrate_legacy_runtime_data(
    paths: RuntimePaths, *, app_directory: Path | None = None
) -> list[str]:
    """Copy legacy source-directory state once; never overwrite current runtime state."""
    source_dir = APP_DIRECTORY if app_directory is None else app_directory
    if source_dir.resolve() == paths.data_dir.resolve():
        return []
    paths.ensure()
    migrated: list[str] = []
    legacy_database = source_dir / "dashboard_data.sqlite3"
    if legacy_database.exists() and not paths.database.exists():
        handle, temporary_name = tempfile.mkstemp(
            prefix="dashboard-migration-", suffix=".sqlite3", dir=paths.data_dir
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            with (
                closing(sqlite3.connect(legacy_database, timeout=10)) as source,
                closing(sqlite3.connect(temporary)) as destination,
            ):
                source.backup(destination)
            os.replace(temporary, paths.database)
        finally:
            if temporary.exists():
                temporary.unlink()
        migrated.append("dashboard_data.sqlite3")
    legacy_settings = source_dir / "collector_settings.json"
    if legacy_settings.exists() and not paths.collector_settings.exists():
        shutil.copy2(legacy_settings, paths.collector_settings)
        migrated.append("collector_settings.json")
    return migrated
