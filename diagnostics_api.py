from __future__ import annotations

import time
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from collector_diagnostics import redact


class DiagnosticLog(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    record_uuid: UUID
    timestamp: float = Field(gt=0, lt=253402300799)
    level: Literal["DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL"]
    event: str = Field(min_length=1, max_length=100)
    message: str = Field(max_length=2000)


class DiagnosticBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site_id: str = Field(min_length=1, max_length=200)
    logs: list[DiagnosticLog] = Field(min_length=1, max_length=100)


def register_diagnostic_routes(app, connect, manager, edge_identity) -> None:
    @app.post("/api/v1/edge/diagnostics/batch")
    def ingest(payload: DiagnosticBatch, identity=Depends(edge_identity)):
        if payload.site_id != identity["site_id"]:
            raise HTTPException(403, "Diagnostic site does not match edge identity")
        customer, site = identity["customer_id"], identity["site_id"]
        with connect() as db:
            # Serialize retention and inserts for this node, including duplicate retries.
            if not db.execute("SELECT id FROM sites WHERE id=%s AND customer_id=%s FOR UPDATE",
                              (site, customer)).fetchone():
                raise HTTPException(404, "Collector node not found")
            for item in payload.logs:
                db.execute("""INSERT INTO collector_diagnostic_logs
                    (customer_id,site_id,record_uuid,instance_id,measured_at,level,event,message)
                    VALUES(%s,%s,%s,%s,to_timestamp(%s),%s,%s,%s)
                    ON CONFLICT(customer_id,site_id,record_uuid) DO NOTHING""",
                    (customer, site, item.record_uuid, identity["instance_id"], item.timestamp,
                     item.level, redact(item.event), redact(item.message)))
            db.execute("""DELETE FROM collector_diagnostic_logs WHERE customer_id=%s AND site_id=%s
                AND (received_at < now()-interval '30 days' OR id IN
                (SELECT id FROM collector_diagnostic_logs WHERE customer_id=%s AND site_id=%s
                 ORDER BY id DESC OFFSET 50000))""", (customer, site, customer, site))
        return {"ok": True, "accepted": [str(item.record_uuid) for item in payload.logs]}

    @app.get("/api/admin/sites/{site_id}/diagnostics")
    def query_logs(site_id: str, hours: int = Query(24, ge=1, le=720),
                   level: Literal["", "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL"] = "",
                   q: str = Query("", max_length=100), before: int | None = Query(None, gt=0),
                   limit: int = Query(100, ge=1, le=200), user=Depends(manager)):
        customer = str(user["customer_id"])
        with connect() as db:
            if not db.execute("SELECT id FROM sites WHERE id=%s AND customer_id=%s", (site_id, customer)).fetchone():
                raise HTTPException(404, "Collector node not found")
            where = ["customer_id=%s", "site_id=%s", "received_at>=to_timestamp(%s)"]
            params: list[object] = [customer, site_id, time.time() - hours * 3600]
            if level:
                where.append("level=%s")
                params.append(level)
            if q:
                where.append("position(lower(%s) in lower(event || ' ' || message))>0")
                params.append(q)
            if before:
                where.append("id<%s")
                params.append(before)
            rows = db.execute("SELECT id,measured_at,received_at,level,event,message,instance_id FROM collector_diagnostic_logs WHERE "
                              + " AND ".join(where) + " ORDER BY id DESC LIMIT %s", (*params, limit + 1)).fetchall()
        items = [{"id": row[0], "timestamp": row[1].timestamp(), "received_at": row[2].timestamp(),
                  "level": row[3], "event": row[4], "message": row[5], "instance_id": row[6]}
                 for row in rows[:limit]]
        return {"ok": True, "data": {"items": items,
                "next_before": items[-1]["id"] if len(rows) > limit else None}}
