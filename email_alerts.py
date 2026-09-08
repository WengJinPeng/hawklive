"""Cloud-only, durable email notifications. No SMTP work in ingest requests."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import smtplib
import ssl
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from uuid import uuid4

ADDRESS = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
MAX_ATTEMPTS = 5
LEASE_SECONDS = 300


def validate_recipients(values: list[str]) -> list[str]:
    result = []
    for value in values:
        value = value.strip()
        if (
            len(value) > 254
            or not ADDRESS.fullmatch(value)
            or len(value.split("@")[0]) > 64
        ):
            raise ValueError("Invalid email address")
        if value.casefold() not in {item.casefold() for item in result}:
            result.append(value)
    if len(result) > 10:
        raise ValueError("At most 10 recipients are allowed")
    return result


def smtp_config() -> dict:
    host = os.environ.get("DCP_SMTP_HOST", "").strip()
    sender = os.environ.get("DCP_SMTP_FROM", "").strip()
    mode = os.environ.get("DCP_SMTP_SECURITY", "starttls")
    port = int(os.environ.get("DCP_SMTP_PORT", "587"))
    username = os.environ.get("DCP_SMTP_USERNAME", "")
    password = os.environ.get("DCP_SMTP_PASSWORD", "")
    if not host or mode not in {"starttls", "ssl"} or not 1 <= port <= 65535:
        raise ValueError("SMTP is not configured")
    validate_recipients([sender])
    if bool(username) != bool(password):
        raise ValueError("SMTP credentials are incomplete")
    return {
        "host": host,
        "port": port,
        "sender": sender,
        "mode": mode,
        "username": username,
        "password": password,
    }


def smtp_ready() -> bool:
    try:
        smtp_config()
        return True
    except (ValueError, TypeError):
        return False


def settings_data(db, customer_id: str) -> dict:
    row = db.execute(
        "SELECT enabled,recipients,notify_recovery FROM email_alert_settings WHERE customer_id=%s",
        (customer_id,),
    ).fetchone()
    worker = db.execute(
        """SELECT heartbeat_at>now()-(%s * interval '1 second') AND smtp_configured
           FROM email_worker_status WHERE singleton=true""",
        (LEASE_SECONDS,),
    ).fetchone()
    return {
        "enabled": bool(row[0]) if row else False,
        "recipients": row[1] if row else [],
        "notify_recovery": bool(row[2]) if row else True,
        "smtp_configured": smtp_ready(),
        "worker_available": bool(worker and worker[0]),
    }


def record_worker_heartbeat(connect) -> None:
    with connect() as db:
        db.execute(
            """INSERT INTO email_worker_status(singleton,heartbeat_at,smtp_configured)
               VALUES(true,now(),%s) ON CONFLICT(singleton) DO UPDATE
               SET heartbeat_at=excluded.heartbeat_at,smtp_configured=excluded.smtp_configured""",
            (smtp_ready(),),
        )


def enqueue(
    db, customer_id: str, kind: str, recipient: str, payload: dict, event_uuid=None
) -> None:
    db.execute(
        """INSERT INTO email_notifications(id,customer_id,event_uuid,kind,recipient,payload)
           VALUES(%s,%s,%s,%s,%s,%s::jsonb)
           ON CONFLICT(customer_id,event_uuid,kind,recipient) DO NOTHING""",
        (
            uuid4(),
            customer_id,
            event_uuid,
            kind,
            recipient,
            json.dumps(payload, ensure_ascii=False),
        ),
    )


def device_email_suppressed(db, customer_id, event_uuid) -> bool:
    # Check both current maintenance and the event's original start time: late
    # uploads must not backfill notifications for a maintenance-period alarm.
    row = db.execute(
        """SELECT NOT d.enabled OR EXISTS (
             SELECT 1 FROM device_maintenance_periods m
             WHERE m.customer_id=a.customer_id AND m.device_id=a.device_id
               AND ((m.started_at<=now() AND LEAST(m.ends_at,COALESCE(m.ended_at,m.ends_at))>now())
                 OR (m.started_at<=COALESCE(a.ended_at,'infinity'::timestamptz) AND LEAST(m.ends_at,COALESCE(m.ended_at,m.ends_at))>a.started_at)))
           FROM alarm_events a JOIN devices d ON d.id=a.device_id AND d.customer_id=a.customer_id
           WHERE a.customer_id=%s AND a.event_uuid=%s""", (customer_id,event_uuid),
    ).fetchone()
    return not row or bool(row[0])


def enqueue_alarm(db, customer_id: str, event_uuid) -> None:
    if device_email_suppressed(db, customer_id, event_uuid):
        return
    # Read the persisted event, not the incoming copy: recovery cannot be undone
    # by an out-of-order retry. Lock policy so a concurrent disable wins cleanly.
    policy = db.execute(
        """SELECT recipients,notify_recovery,enabled_since FROM email_alert_settings
           WHERE customer_id=%s AND enabled=true FOR SHARE""",
        (customer_id,),
    ).fetchone()
    if not policy:
        return
    row = db.execute(
        """SELECT a.source,a.started_at,a.ended_at,a.metric,a.trigger_value,a.peak_value,
                  a.limit_description,r.name,d.name
           FROM alarm_events a JOIN cleanrooms r ON r.id=a.cleanroom_id AND r.customer_id=a.customer_id
           JOIN devices d ON d.id=a.device_id AND d.customer_id=a.customer_id AND d.enabled=true
           WHERE a.event_uuid=%s AND a.customer_id=%s""",
        (event_uuid, customer_id),
    ).fetchone()
    if not row or row[0] != "device" or row[1] < policy[2]:
        return  # Never backfill pre-subscription or simulated events.
    kind = "recovery" if row[2] else "alarm"
    if kind == "recovery":
        # Do not deliver an obsolete active-state email after a recovery.
        db.execute(
            """UPDATE email_notifications SET status='cancelled'
                      WHERE customer_id=%s AND event_uuid=%s AND kind='alarm' AND status='pending'""",
            (customer_id, event_uuid),
        )
        if not policy[1]:
            return
    payload = {
        "event_uuid": str(event_uuid),
        "started_at": row[1].isoformat(),
        "ended_at": row[2].isoformat() if row[2] else None,
        "metric": row[3],
        "trigger_value": row[4],
        "peak_value": row[5],
        "limit": row[6],
        "room": row[7],
        "device": row[8],
    }
    for recipient in policy[0]:
        enqueue(db, customer_id, kind, recipient, payload, event_uuid)


def build_message(job: dict, sender: str) -> EmailMessage:
    message = EmailMessage()
    title = {
        "alarm": "报警 / Alarm",
        "recovery": "恢复 / Recovery",
        "test": "测试 / Test",
    }[job["kind"]]
    message["Subject"] = f"[HawkHive] {title}"
    message["From"] = sender
    message["To"] = job["recipient"]
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = f"<{job['id']}@{sender.split('@')[1]}>"
    payload = job["payload"]
    lines = [f"HawkHive 电邮预警 / Email notification — {title}", ""]
    if job["kind"] == "test":
        lines.append(
            "这是一封测试邮件，不代表设备发生报警。 / Test only; no device alarm."
        )
    else:
        for key, label in (
            ("room", "洁净室 / Cleanroom"),
            ("device", "设备 / Device"),
            ("metric", "通道 / Metric"),
            ("trigger_value", "触发值 / Trigger"),
            ("peak_value", "峰值 / Peak"),
            ("limit", "阈值 / Limit"),
            ("started_at", "开始时间 / Started"),
            ("ended_at", "恢复时间 / Recovered"),
            ("event_uuid", "事件编号 / Event ID"),
        ):
            lines.append(
                f"{label}: {payload.get(key) if payload.get(key) is not None else '—'}"
            )
        lines.extend(
            [
                "",
                "时间包含时区；此邮件为事件记录，不代表当前实时状态。",
                "Times include timezone. This records an event, not the current live state.",
            ]
        )
    message.set_content("\n".join(lines))
    return message


def send_notification(job: dict) -> None:
    config = smtp_config()
    message = build_message(job, config["sender"])
    context = ssl.create_default_context()
    client = (
        smtplib.SMTP_SSL(config["host"], config["port"], timeout=15, context=context)
        if config["mode"] == "ssl"
        else smtplib.SMTP(config["host"], config["port"], timeout=15)
    )
    try:
        if config["mode"] == "starttls":
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        if config["username"]:
            client.login(config["username"], config["password"])
        refused = client.send_message(
            message, from_addr=config["sender"], to_addrs=[job["recipient"]]
        )
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        # QUIT failure after DATA acceptance must not manufacture a failed send.
        client.close()


def claim_notification(connect) -> dict | None:
    """Commit a bounded claim before network I/O; only its token may finish it."""
    with connect() as db:
        row = db.execute(
            """SELECT id,customer_id,kind,recipient,payload,attempts,event_uuid,status
               FROM email_notifications WHERE
                 (status='pending' AND next_attempt_at<=now())
                 OR (status='sending' AND lease_until<=now())
               ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""",
        ).fetchone()
        if not row:
            return None
        job = dict(
            zip(
                (
                    "id",
                    "customer_id",
                    "kind",
                    "recipient",
                    "payload",
                    "attempts",
                    "event_uuid",
                    "status",
                ),
                row,
            )
        )
        policy = settings_data(db, job["customer_id"])
        cancelled = job["recipient"] not in policy["recipients"] or (
            job["kind"] != "test"
            and (
                not policy["enabled"]
                or (job["kind"] == "recovery" and not policy["notify_recovery"])
            )
        )
        if job["kind"] == "alarm":
            event = db.execute(
                "SELECT ended_at FROM alarm_events WHERE event_uuid=%s AND customer_id=%s",
                (job["event_uuid"], job["customer_id"]),
            ).fetchone()
            cancelled = cancelled or not event or event[0] is not None
        if job["kind"] != "test":
            cancelled = cancelled or device_email_suppressed(db, job["customer_id"], job["event_uuid"])
        if cancelled:
            uncertain = job["status"] == "sending"
            db.execute(
                """UPDATE email_notifications SET status=%s,last_error=%s,
                   claim_token=NULL,lease_until=NULL WHERE id=%s""",
                (
                    "failed" if uncertain else "cancelled",
                    "DeliveryOutcomeUnknown" if uncertain else None,
                    job["id"],
                ),
            )
            return {"skipped": True}
        if job["attempts"] >= MAX_ATTEMPTS:
            db.execute(
                """UPDATE email_notifications SET status='failed',last_error='DeliveryOutcomeUnknown',
                   claim_token=NULL,lease_until=NULL WHERE id=%s""",
                (job["id"],),
            )
            return {"skipped": True}
        job["attempts"] += 1
        job["claim_token"] = uuid4()
        db.execute(
            """UPDATE email_notifications SET status='sending',attempts=%s,claim_token=%s,
               lease_until=now()+(%s * interval '1 second') WHERE id=%s""",
            (job["attempts"], job["claim_token"], LEASE_SECONDS, job["id"]),
        )
        return job


def finish_notification(connect, job: dict, error: str | None = None) -> bool:
    with connect() as db:
        status = (
            "sent"
            if error is None
            else "failed"
            if job["attempts"] >= MAX_ATTEMPTS
            else "pending"
        )
        if error is not None:
            # A disable/recovery can finish while SMTP is in flight. Do not put
            # its failed attempt back on a queue that the user just cancelled.
            db.execute(
                "SELECT customer_id FROM email_alert_settings WHERE customer_id=%s FOR SHARE",
                (job["customer_id"],),
            )
            policy = settings_data(db, job["customer_id"])
            cancelled = job["recipient"] not in policy["recipients"] or (
                job["kind"] != "test"
                and (
                    not policy["enabled"]
                    or (job["kind"] == "recovery" and not policy["notify_recovery"])
                )
            )
            if job["kind"] == "alarm":
                event = db.execute(
                    "SELECT ended_at FROM alarm_events WHERE event_uuid=%s AND customer_id=%s",
                    (job["event_uuid"], job["customer_id"]),
                ).fetchone()
                cancelled = cancelled or not event or event[0] is not None
            if job["kind"] != "test":
                cancelled = cancelled or device_email_suppressed(db, job["customer_id"], job["event_uuid"])
            if cancelled:
                status = "cancelled"
        result = db.execute(
            """UPDATE email_notifications SET status=%s,last_error=%s,
               sent_at=CASE WHEN %s THEN now() ELSE sent_at END,
               next_attempt_at=now()+(%s * interval '1 second'),claim_token=NULL,lease_until=NULL
               WHERE id=%s AND status='sending' AND claim_token=%s""",
            (
                status,
                error,
                error is None,
                min(3600, 60 * 2 ** (job["attempts"] - 1)),
                job["id"],
                job["claim_token"],
            ),
        )
        return result.rowcount == 1


def deliver_one(connect, sender=send_notification) -> bool:
    job = claim_notification(connect)
    if job is None:
        return False
    if job.get("skipped"):
        return True
    error = None
    try:
        sender(job)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Bad payloads cannot crash-loop the worker. Never store raw SMTP replies.
        error = type(exc).__name__
    finish_notification(connect, job, error)
    return True


def main() -> None:
    from psycopg import Error as DatabaseError

    from cloud_api import connect

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_args: stop.set())
    while not stop.is_set():
        try:
            record_worker_heartbeat(connect)
            if smtp_ready() and deliver_one(connect):
                continue
        except (DatabaseError, OSError, ValueError) as exc:
            logging.getLogger(__name__).error("Email worker: %s", type(exc).__name__)
        stop.wait(5)


if __name__ == "__main__":
    main()
