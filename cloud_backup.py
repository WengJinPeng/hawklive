from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def secure_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)


def prune_daily_backups(directory: Path, retention_days: int, now: datetime) -> list[Path]:
    """Remove only expired daily dump/checksum pairs from the configured directory."""
    cutoff = (now - timedelta(days=max(1, retention_days))).timestamp()
    removed: list[Path] = []
    for backup in sorted(directory.glob("dcp8001_*.dump")):
        if backup.stat().st_mtime >= cutoff:
            continue
        checksum = backup.with_suffix(".dump.sha256")
        backup.unlink()
        checksum.unlink(missing_ok=True)
        removed.append(backup)
    return removed


def monthly_backup_exists(directory: Path, now: datetime) -> bool:
    return any(directory.glob(f"dcp8001_{now:%Y-%m}-*.dump"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Create non-deleting PostgreSQL backups.")
    parser.add_argument("--daily-dir", default=os.environ.get("DCP_DAILY_BACKUP_DIR", "backups/daily"))
    parser.add_argument("--monthly-dir", default=os.environ.get("DCP_MONTHLY_BACKUP_DIR", ""))
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        raise SystemExit("pg_dump was not found")

    daily_dir = Path(args.daily_dir).resolve()
    secure_directory(daily_dir)
    timezone_name = os.environ.get("DCP_BACKUP_TIMEZONE", "Asia/Singapore")
    try:
        now = datetime.now(ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError as exc:
        raise SystemExit(f"Unknown DCP_BACKUP_TIMEZONE: {timezone_name}") from exc
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    backup = daily_dir / f"dcp8001_{stamp}.dump"
    subprocess.run(
        [
            pg_dump, "--dbname", database_url, "--format=custom", "--compress=9",
            "--file", str(backup),
        ],
        check=True,
    )
    secure_file(backup)
    checksum = sha256(backup)
    checksum_path = backup.with_suffix(".dump.sha256")
    checksum_path.write_text(f"{checksum}  {backup.name}\n", encoding="ascii")
    secure_file(checksum_path)
    print(f"Daily backup: {backup}")

    if args.monthly_dir:
        monthly_dir = Path(args.monthly_dir).resolve()
        secure_directory(monthly_dir)
        if not monthly_backup_exists(monthly_dir, now):
            monthly = monthly_dir / backup.name
            shutil.copy2(backup, monthly)
            shutil.copy2(checksum_path, monthly.with_suffix(".dump.sha256"))
            print(f"Monthly independent copy: {monthly}")

    retention_days = max(1, int(os.environ.get("DCP_DAILY_BACKUP_RETENTION_DAYS", "14")))
    removed = prune_daily_backups(daily_dir, retention_days, now)
    if removed:
        print(f"Pruned {len(removed)} daily backup(s) older than {retention_days} days")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
