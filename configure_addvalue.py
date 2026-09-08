from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).with_name("dashboard_data.sqlite3")


def main() -> int:
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        threshold_columns = {row[1] for row in db.execute("PRAGMA table_info(thresholds)")}
        if "particle_0_5_max" not in threshold_columns:
            db.execute(
                "ALTER TABLE thresholds ADD COLUMN particle_0_5_max REAL NOT NULL DEFAULT 100000"
            )
            db.execute("UPDATE thresholds SET particle_0_5_max = particle_5_max")
        db.execute("UPDATE customers SET name = 'Addvalue' WHERE id = 'customer-001'")
        db.execute("UPDATE users SET display_name = 'Addvalue' WHERE customer_id = 'customer-001'")
        db.execute("UPDATE cleanrooms SET name = 'Cleanroom 1' WHERE id = 'room-001'")
        db.execute("UPDATE devices SET name = 'Device 1', enabled = 1 WHERE id = 'device-001'")
        db.execute(
            """
            UPDATE thresholds SET profile_name = 'Class 100K', particle_0_5_max = 100000,
                temperature_min = 18, temperature_max = 25,
                humidity_min = 40, humidity_max = 70, alarm_delay_seconds = 300
            WHERE cleanroom_id = 'room-001'
            """
        )
        db.execute("DELETE FROM devices WHERE id IN ('device-002', 'device-003')")
        db.execute("DELETE FROM thresholds WHERE cleanroom_id = 'room-002'")
        db.execute("DELETE FROM cleanrooms WHERE id = 'room-002'")
        db.execute("DELETE FROM alarm_states")
        row = db.execute(
            """
            SELECT customers.name AS customer, cleanrooms.name AS cleanroom,
                devices.name AS device, thresholds.profile_name,
                thresholds.particle_0_5_max, thresholds.temperature_min,
                thresholds.temperature_max, thresholds.humidity_min,
                thresholds.humidity_max
            FROM customers
            JOIN cleanrooms ON cleanrooms.customer_id = customers.id
            JOIN devices ON devices.cleanroom_id = cleanrooms.id
            JOIN thresholds ON thresholds.cleanroom_id = cleanrooms.id
            WHERE customers.id = 'customer-001'
            """
        ).fetchone()
        print(json.dumps(dict(row), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
