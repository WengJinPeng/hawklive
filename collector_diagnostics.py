"""Bounded, tenant-bound diagnostic queue. Never sends data itself."""
from __future__ import annotations

import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

LOCAL_LOG_LIMIT = 20_000
LOCAL_LOG_SECONDS = 7 * 86400
log_scope = ContextVar("diagnostic_log_scope", default=None)


@contextmanager
def diagnostic_scope(customer_id, site_id):
    token = log_scope.set((customer_id, site_id))
    try:
        yield
    finally:
        log_scope.reset(token)


def redact(message: object, secrets: tuple[str, ...] = ()) -> str:
    text = str(message)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[^\s,;\"']+", "[REDACTED AUTH]", text)
    text = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    # Remove whole query strings: signed URLs often use vendor-specific key names.
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[REDACTED]", text)
    text = re.sub(
        r'''(?ix)(["']?(?:[\w-]*(?:token|password|secret|api[_-]?key)|authorization|cookie|set-cookie)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''',
        r"\1[REDACTED]", text,
    )
    return text.replace("\x00", "")[:2000]


def init_queue(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS diagnostic_outbox (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, record_uuid TEXT UNIQUE NOT NULL,
        customer_id TEXT NOT NULL, site_id TEXT NOT NULL, instance_id TEXT NOT NULL,
        timestamp REAL NOT NULL, level TEXT NOT NULL, event TEXT NOT NULL,
        message TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_diagnostic_scope ON diagnostic_outbox(customer_id,site_id,seq)")
    db.execute("CREATE TABLE IF NOT EXISTS diagnostic_queue_stats (id INTEGER PRIMARY KEY, discarded INTEGER NOT NULL)")
    db.execute("INSERT OR IGNORE INTO diagnostic_queue_stats VALUES(1,0)")


def enqueue(db, customer_id: str, site_id: str, instance_id: str,
            level: str, event: str, message: str) -> None:
    db.execute("""INSERT INTO diagnostic_outbox
        (record_uuid,customer_id,site_id,instance_id,timestamp,level,event,message)
        VALUES(?,?,?,?,?,?,?,?)""",
        (str(uuid4()), customer_id, site_id, instance_id, time.time(),
         level.upper()[:16], redact(event)[:100], redact(message)))
    prune_queue(db)


def prune_queue(db) -> None:
    expired = db.execute("DELETE FROM diagnostic_outbox WHERE timestamp < ?", (time.time() - LOCAL_LOG_SECONDS,)).rowcount
    overflow = db.execute("""DELETE FROM diagnostic_outbox WHERE seq IN
        (SELECT seq FROM diagnostic_outbox ORDER BY seq DESC LIMIT -1 OFFSET ?)""", (LOCAL_LOG_LIMIT,)).rowcount
    if expired + overflow:
        db.execute("UPDATE diagnostic_queue_stats SET discarded=discarded+? WHERE id=1", (expired + overflow,))
