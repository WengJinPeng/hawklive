"""Tenant-scoped permanent data cleanup and empty-workshop deletion."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field


class DataCleanupRequest(BaseModel):
    scope: Literal["retired_devices", "all_monitoring"]
    confirmation: str = Field(min_length=1, max_length=80)


CONFIRMATIONS = {
    "retired_devices": "PURGE RETIRED",
    "all_monitoring": "PURGE ALL",
}


def cleanup_preview(db: Any, customer_id: str) -> dict[str, dict[str, int]]:
    retired = db.execute(
        """
        SELECT
          (SELECT count(*) FROM devices d WHERE d.customer_id=%s AND NOT d.enabled),
          (SELECT count(*) FROM readings r WHERE r.customer_id=%s AND EXISTS (
             SELECT 1 FROM devices d WHERE d.customer_id=r.customer_id AND d.id=r.device_id AND NOT d.enabled)),
          (SELECT count(*) FROM latest_readings l WHERE l.customer_id=%s AND EXISTS (
             SELECT 1 FROM devices d WHERE d.customer_id=l.customer_id AND d.id=l.device_id AND NOT d.enabled)),
          (SELECT count(*) FROM alarm_events a WHERE a.customer_id=%s AND EXISTS (
             SELECT 1 FROM devices d WHERE d.customer_id=a.customer_id AND d.id=a.device_id AND NOT d.enabled)),
          (SELECT count(*) FROM email_notifications n WHERE n.customer_id=%s AND EXISTS (
             SELECT 1 FROM alarm_events a JOIN devices d ON d.customer_id=a.customer_id AND d.id=a.device_id
             WHERE a.customer_id=n.customer_id AND a.event_uuid=n.event_uuid AND NOT d.enabled))
        """,
        (customer_id, customer_id, customer_id, customer_id, customer_id),
    ).fetchone()
    global_counts = db.execute(
        """
        SELECT
          (SELECT count(*) FROM readings WHERE customer_id=%s),
          (SELECT count(*) FROM latest_readings WHERE customer_id=%s),
          (SELECT count(*) FROM alarm_events WHERE customer_id=%s),
          (SELECT count(*) FROM email_notifications WHERE customer_id=%s)
        """,
        (customer_id, customer_id, customer_id, customer_id),
    ).fetchone()
    retired_result = {
        "devices": int(retired[0]), "readings": int(retired[1]),
        "latest": int(retired[2]), "alarms": int(retired[3]),
        "notifications": int(retired[4]),
    }
    all_result = {
        "devices": 0, "readings": int(global_counts[0]),
        "latest": int(global_counts[1]), "alarms": int(global_counts[2]),
        "notifications": int(global_counts[3]),
    }
    retired_result["total"] = sum(retired_result.values())
    all_result["total"] = sum(all_result.values())
    return {"retired_devices": retired_result, "all_monitoring": all_result}


def register_data_management(api: Any) -> None:
    app = api.app

    @app.get("/api/admin/data-cleanup/preview")
    def data_cleanup_preview(
        user: dict = Depends(api.primary_data_manager),
    ) -> dict[str, object]:
        with api.connect() as db:
            data = cleanup_preview(db, str(user["customer_id"]))
        return {"ok": True, "data": data}

    @app.post("/api/admin/data-cleanup")
    def data_cleanup(
        payload: DataCleanupRequest,
        user: dict = Depends(api.primary_data_manager),
    ) -> dict[str, object]:
        expected = CONFIRMATIONS[payload.scope]
        if payload.confirmation.strip() != expected:
            raise HTTPException(400, f'Type "{expected}" to confirm permanent deletion')
        customer_id = str(user["customer_id"])
        with api.connect() as db:
            db.execute("SELECT 1 FROM customers WHERE id=%s FOR UPDATE", (customer_id,))
            before = cleanup_preview(db, customer_id)[payload.scope]
            if payload.scope == "retired_devices":
                retired_ids = [str(row[0]) for row in db.execute(
                    "SELECT id FROM devices WHERE customer_id=%s AND NOT enabled FOR UPDATE",
                    (customer_id,),
                ).fetchall()]
                if retired_ids:
                    db.execute(
                        """DELETE FROM email_notifications n WHERE n.customer_id=%s AND EXISTS (
                           SELECT 1 FROM alarm_events a WHERE a.customer_id=n.customer_id
                             AND a.event_uuid=n.event_uuid AND a.device_id=ANY(%s))""",
                        (customer_id, retired_ids),
                    )
                    db.execute("DELETE FROM alarm_events WHERE customer_id=%s AND device_id=ANY(%s)", (customer_id, retired_ids))
                    db.execute("DELETE FROM latest_readings WHERE customer_id=%s AND device_id=ANY(%s)", (customer_id, retired_ids))
                    db.execute("DELETE FROM readings WHERE customer_id=%s AND device_id=ANY(%s)", (customer_id, retired_ids))
                    db.execute("DELETE FROM device_registration_links WHERE customer_id=%s AND (duplicate_device_id=ANY(%s) OR canonical_device_id=ANY(%s))", (customer_id, retired_ids, retired_ids))
                    db.execute("DELETE FROM device_maintenance_periods WHERE customer_id=%s AND device_id=ANY(%s)", (customer_id, retired_ids))
                    db.execute("DELETE FROM device_assignment_periods WHERE customer_id=%s AND device_id=ANY(%s)", (customer_id, retired_ids))
                    db.execute("DELETE FROM devices WHERE customer_id=%s AND NOT enabled AND id=ANY(%s)", (customer_id, retired_ids))
            else:
                db.execute("DELETE FROM email_notifications WHERE customer_id=%s", (customer_id,))
                db.execute("DELETE FROM alarm_events WHERE customer_id=%s", (customer_id,))
                db.execute("DELETE FROM latest_readings WHERE customer_id=%s", (customer_id,))
                db.execute("DELETE FROM readings WHERE customer_id=%s", (customer_id,))
            api.record_configuration_audit(
                db, customer_id, "customer_user", f"data_cleanup.{payload.scope}",
                "customer", customer_id, {"deleted": before, "permanent": True},
                str(user["id"]),
            )
            remaining = cleanup_preview(db, customer_id)
        return {"ok": True, "data": {"deleted": before, "remaining": remaining}}

    @app.delete("/api/admin/cleanrooms/{cleanroom_id}")
    def delete_cleanroom(
        cleanroom_id: str,
        expected_name: str = Query(min_length=1, max_length=100),
        user: dict = Depends(api.primary_data_manager),
    ) -> dict[str, object]:
        customer_id = str(user["customer_id"])
        with api.connect() as db:
            db.execute("SELECT 1 FROM customers WHERE id=%s FOR UPDATE", (customer_id,))
            room = db.execute(
                "SELECT name FROM cleanrooms WHERE id=%s AND customer_id=%s FOR UPDATE",
                (cleanroom_id, customer_id),
            ).fetchone()
            if room is None:
                raise HTTPException(404, "Workshop not found")
            if str(room[0]) != expected_name.strip():
                raise HTTPException(409, "Workshop name confirmation does not match")
            active, retired = db.execute(
                """SELECT count(*) FILTER (WHERE enabled),count(*) FILTER (WHERE NOT enabled)
                   FROM devices WHERE customer_id=%s AND cleanroom_id=%s""",
                (customer_id, cleanroom_id),
            ).fetchone()
            if active:
                raise HTTPException(409, "Delete or move active devices before deleting this workshop")
            if retired:
                raise HTTPException(409, "Purge retired-device data before deleting this workshop")
            readings, latest, alarms = db.execute(
                """SELECT
                   (SELECT count(*) FROM readings WHERE customer_id=%s AND cleanroom_id=%s),
                   (SELECT count(*) FROM latest_readings WHERE customer_id=%s AND cleanroom_id=%s),
                   (SELECT count(*) FROM alarm_events WHERE customer_id=%s AND cleanroom_id=%s)""",
                (customer_id, cleanroom_id, customer_id, cleanroom_id, customer_id, cleanroom_id),
            ).fetchone()
            if readings or latest or alarms:
                raise HTTPException(409, "Clean monitoring data for this workshop before deleting it")
            db.execute(
                "UPDATE device_discovery_jobs SET cleanroom_id=NULL WHERE customer_id=%s AND cleanroom_id=%s",
                (customer_id, cleanroom_id),
            )
            db.execute(
                "DELETE FROM device_assignment_periods WHERE customer_id=%s AND cleanroom_id=%s AND ended_at IS NOT NULL",
                (customer_id, cleanroom_id),
            )
            db.execute("DELETE FROM thresholds WHERE customer_id=%s AND cleanroom_id=%s", (customer_id, cleanroom_id))
            db.execute("DELETE FROM cleanrooms WHERE customer_id=%s AND id=%s", (customer_id, cleanroom_id))
            db.execute("UPDATE sites SET config_version=config_version+1 WHERE customer_id=%s", (customer_id,))
            api.record_configuration_audit(
                db, customer_id, "customer_user", "cleanroom.deleted", "cleanroom",
                cleanroom_id, {"name": str(room[0]), "permanent": True}, str(user["id"]),
            )
            updated = api.customer_configuration(customer_id, db=db)
        return {"ok": True, "data": updated}
