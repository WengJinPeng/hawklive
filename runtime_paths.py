from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from collector_activation import INSTALLATION_ID_FILENAME

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
    # Existing settings belong to the destination collector. Never combine them
    # with another collector's legacy database or installation identity.
    if paths.collector_settings.exists():
        return []
    legacy_identity = source_dir / INSTALLATION_ID_FILENAME
    target_identity = paths.data_dir / INSTALLATION_ID_FILENAME
    legacy_settings = source_dir / "collector_settings.json"
    if legacy_identity.exists():
        identity = legacy_identity.read_text(encoding="ascii").strip()
        if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise RuntimeError("Legacy collector installation identity is invalid")
        if target_identity.exists() and target_identity.read_text(encoding="ascii").strip() != identity:
            raise RuntimeError("Legacy collector installation identity conflicts with destination")
    elif legacy_settings.exists() and target_identity.exists():
        raise RuntimeError("Legacy collector settings have no matching destination installation identity")
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
    # Publish identity before settings so an interrupted copy can be retried
    # without making a new machine identity for the migrated cloud credentials.
    for source, destination in (
        (legacy_identity, target_identity),
        (legacy_settings, paths.collector_settings),
    ):
        if not source.exists() or destination.exists():
            continue
        handle, temporary_name = tempfile.mkstemp(prefix="collector-migration-", dir=paths.data_dir)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(source.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        migrated.append(destination.name)
    return migrated
