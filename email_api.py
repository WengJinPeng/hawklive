from __future__ import annotations

import json

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from email_alerts import enqueue, settings_data, smtp_ready, validate_recipients


class EmailSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    recipients: list[str] = Field(default_factory=list, max_length=10)
    notify_recovery: bool = True

    @field_validator("recipients")
    @classmethod
    def valid_recipients(cls, values):
        return validate_recipients(values)


def register_email_routes(app, connect, manager, audit) -> None:
    manager_dependency = Depends(manager)

    @app.get("/api/email-alerts")
    def get_settings(user=manager_dependency):
        with connect() as db:
            data = settings_data(db, str(user["customer_id"]))
        return {"ok": True, "data": data}

    @app.put("/api/email-alerts")
    def save_settings(payload: EmailSettings, user=manager_dependency):
        if payload.enabled and not payload.recipients:
            raise HTTPException(400, "At least one recipient is required")
        if payload.enabled and not smtp_ready():
            raise HTTPException(400, "SMTP is not configured")
        customer_id = str(user["customer_id"])
        with connect() as db:
            db.execute(
                """INSERT INTO email_alert_settings(customer_id) VALUES(%s)
                          ON CONFLICT(customer_id) DO NOTHING""",
                (customer_id,),
            )
            db.execute(
                "SELECT customer_id FROM email_alert_settings WHERE customer_id=%s FOR UPDATE",
                (customer_id,),
            )
            before = settings_data(db, customer_id)
            db.execute(
                """UPDATE email_alert_settings SET enabled=%s,recipients=%s::jsonb,notify_recovery=%s,
                   enabled_since=CASE WHEN %s AND NOT enabled THEN now() ELSE enabled_since END,
                   updated_at=now() WHERE customer_id=%s""",
                (
                    payload.enabled,
                    json.dumps(payload.recipients),
                    payload.notify_recovery,
                    payload.enabled,
                    customer_id,
                ),
            )
            db.execute(
                """UPDATE email_notifications SET status='cancelled'
                   WHERE customer_id=%s AND status='pending' AND
                     (NOT (recipient=ANY(%s)) OR (kind<>'test' AND NOT %s)
                      OR (kind='recovery' AND NOT %s))""",
                (
                    customer_id,
                    payload.recipients,
                    payload.enabled,
                    payload.notify_recovery,
                ),
            )
            audit(
                db,
                customer_id,
                "customer_user",
                "email_alerts_updated",
                "email_alert_settings",
                customer_id,
                {"before": before, "after": payload.model_dump()},
                str(user["id"]),
            )
            data = settings_data(db, customer_id)
        return {"ok": True, "data": data}

    @app.post("/api/email-alerts/test", status_code=202)
    def test_email(user=manager_dependency):
        customer_id = str(user["customer_id"])
        if not smtp_ready():
            raise HTTPException(400, "SMTP is not configured")
        with connect() as db:
            row = db.execute(
                """SELECT recipients,(last_test_at>now()-interval '60 seconds')
                   FROM email_alert_settings WHERE customer_id=%s FOR UPDATE""",
                (customer_id,),
            ).fetchone()
            if not row or not row[0]:
                raise HTTPException(400, "Save recipients before sending a test")
            if row[1]:
                raise HTTPException(429, "Wait 60 seconds before sending another test")
            for recipient in row[0]:
                enqueue(db, customer_id, "test", recipient, {})
            db.execute(
                "UPDATE email_alert_settings SET last_test_at=now() WHERE customer_id=%s",
                (customer_id,),
            )
            audit(
                db,
                customer_id,
                "customer_user",
                "email_test_queued",
                "email_alert_settings",
                customer_id,
                {"recipient_count": len(row[0])},
                str(user["id"]),
            )
        return {"ok": True, "data": {"queued": len(row[0])}}

    @app.get("/api/email-alerts/history")
    def history(user=manager_dependency):
        with connect() as db:
            rows = db.execute(
                """SELECT id,kind,recipient,status,attempts,last_error,created_at,sent_at,
                          event_uuid,payload,next_attempt_at
                   FROM email_notifications WHERE customer_id=%s
                   ORDER BY created_at DESC,id DESC LIMIT 30""",
                (str(user["customer_id"]),),
            ).fetchall()
        return {
            "ok": True,
            "data": [
                {
                    "id": str(r[0]),
                    "kind": r[1],
                    "recipient": r[2],
                    "status": r[3],
                    "attempts": r[4],
                    "last_error": r[5],
                    "created_at": r[6].timestamp(),
                    "sent_at": r[7].timestamp() if r[7] else None,
                    "event_uuid": str(r[8]) if r[8] else None,
                    "room": r[9].get("room") if isinstance(r[9], dict) else None,
                    "device": r[9].get("device") if isinstance(r[9], dict) else None,
                    "metric": r[9].get("metric") if isinstance(r[9], dict) else None,
                    "next_attempt_at": r[10].timestamp() if r[3] == "pending" else None,
                }
                for r in rows
            ],
        }
