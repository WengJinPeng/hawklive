from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import time
from pathlib import Path

from cloud_api import connect

ARCHIVE_COLUMNS = (
    "record_uuid", "customer_id", "site_id", "cleanroom_id", "cleanroom_name",
    "device_id", "device_name", "measured_at", "received_at", "source", "particles",
    "environment", "alarm_status", "alarm_details", "cleanliness_code", "cleanliness_label",
    "particle_unit_code", "particle_unit_label", "protocol_profile",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a compressed, non-deleting cloud history archive.")
    parser.add_argument("--older-than-days", type=int, default=365)
    parser.add_argument("--output-dir", default=os.environ.get("DCP_ARCHIVE_DIR", "backups/archive"))
    args = parser.parse_args()
    if args.older_than_days < 1:
        raise SystemExit("--older-than-days must be positive")
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    target = output_dir / f"readings_before_{stamp}.jsonl.gz"
    count = 0
    with connect() as db, target.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9) as zipped:
        with db.cursor(name="archive_readings") as cursor:
            cursor.execute(
                """
                SELECT record_uuid, customer_id, site_id, cleanroom_id, cleanroom_name,
                    device_id, device_name, measured_at, received_at, source, particles,
                    environment, alarm_status, alarm_details, cleanliness_code, cleanliness_label,
                    particle_unit_code, particle_unit_label, protocol_profile
                FROM readings
                WHERE measured_at < now() - (%s * interval '1 day')
                ORDER BY measured_at
                """,
                (args.older_than_days,),
            )
            for row in cursor:
                zipped.write((json.dumps(list(row), ensure_ascii=False, default=str) + "\n").encode("utf-8"))
                count += 1
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {
        "file": target.name,
        "sha256": checksum,
        "records": count,
        "rows_deleted": 0,
        "format": "json-array",
        "columns": list(ARCHIVE_COLUMNS),
    }
    target.with_suffix(".json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
