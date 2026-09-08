from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_backup_time(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, TypeError) as exc:
        raise ValueError("DCP_BACKUP_TIME must use HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("DCP_BACKUP_TIME must use a valid 24-hour time")
    return hour, minute


def next_backup_time(now: datetime, hour: int, minute: int) -> datetime:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def backup_exists_for_date(directory: Path, now: datetime) -> bool:
    return any(directory.glob(f"dcp8001_{now:%Y-%m-%d}_*.dump"))


def run_backup(daily_dir: Path, monthly_dir: Path) -> None:
    script = Path(__file__).with_name("cloud_backup.py")
    subprocess.run(
        [
            sys.executable, str(script), "--daily-dir", str(daily_dir),
            "--monthly-dir", str(monthly_dir),
        ],
        check=True,
    )


def main() -> int:
    daily_dir = Path(os.environ.get("DCP_DAILY_BACKUP_DIR", "/app/backups/daily")).resolve()
    monthly_dir = Path(os.environ.get("DCP_MONTHLY_BACKUP_DIR", "/app/monthly-backups")).resolve()
    hour, minute = parse_backup_time(os.environ.get("DCP_BACKUP_TIME", "02:00"))
    timezone_name = os.environ.get("DCP_BACKUP_TIMEZONE", "Asia/Singapore")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise SystemExit(f"Unknown DCP_BACKUP_TIMEZONE: {timezone_name}") from exc
    retry_seconds = max(60, int(os.environ.get("DCP_BACKUP_RETRY_SECONDS", "900")))
    stop = threading.Event()

    def request_stop(*_args: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    daily_dir.mkdir(parents=True, exist_ok=True)
    monthly_dir.mkdir(parents=True, exist_ok=True)
    print(
        f"Backup scheduler ready: daily at {hour:02d}:{minute:02d} {timezone_name}; "
        f"daily={daily_dir}; monthly={monthly_dir}",
        flush=True,
    )

    while not stop.is_set():
        now = datetime.now(timezone)
        if not backup_exists_for_date(daily_dir, now):
            try:
                run_backup(daily_dir, monthly_dir)
            except subprocess.CalledProcessError as exc:
                print(f"Backup failed with exit code {exc.returncode}; retrying in {retry_seconds}s", flush=True)
                stop.wait(retry_seconds)
                continue
        target = next_backup_time(datetime.now(timezone), hour, minute)
        delay = max(1.0, (target - datetime.now(timezone)).total_seconds())
        print(f"Next backup: {target.isoformat()}", flush=True)
        stop.wait(delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
