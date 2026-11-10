"""Optional bounded local index migration. Never updates a reading or its ownership."""
import json
import os
from pathlib import Path
import sqlite3
import time
from update_protocol import atomic_json, read_json

NAME='idx_readings_quarantine_scope_uuid_v1'
SQL=f"CREATE INDEX {NAME} ON readings(customer_id,site_id,record_uuid) WHERE source='device' AND sync_quarantined_at IS NOT NULL"


def usable(db):
    row=db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(NAME,)).fetchone()
    return bool(row and row[0]==SQL)


def table(db):
    return 'readings INDEXED BY '+NAME if usable(db) else 'readings'


def enabled(service):
    if os.environ.get('DCP_UPDATE_TRIAL')=='1':
        journal=os.environ.get('DCP_UPDATE_JOURNAL')
        if not journal or Path(journal).exists():return False
    path=Path(service.db_path).parent/'maintenance-index-policy.json'
    policy=read_json(path)
    expiry=policy.get('expires_at')
    return (policy.get('enabled') is True and type(expiry) in (int,float)
            and time.time()<expiry<=time.time()+72*3600
            and policy.get('scope')=={'customer_id':service.customer_id,'site_id':service.site_id})


def ensure(path, *, budget=.75):
    """At most three attempts, ten-minute backoff, explicit transactional DDL.

    The old query remains available if the index cannot be built within budget.
    This does not relax the one-second report deadline or startup path.
    """
    path=Path(path)
    state_path=path.parent/'maintenance-index-state.json'
    state=read_json(state_path)
    if state_path.exists() and (not state or type(state.get('attempts')) is not int
                                or type(state.get('retry_after',0)) not in (int,float)):return False
    if state.get('attempts',0)>=3 or time.time()<state.get('retry_after',0):return False
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=rw',uri=True,timeout=.05)
    try:
        if usable(db):return True
        if db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(NAME,)).fetchone():return False
        try:
            db.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError:
            atomic_json(state_path,{'attempts':state.get('attempts',0),
                                   'retry_after':time.time()+60,'state':'busy'})
            return False
        attempts=state.get('attempts',0)+1
        atomic_json(state_path,{'attempts':attempts,'retry_after':time.time()+600,'state':'attempting'})
        start=time.monotonic()
        db.set_progress_handler(lambda:int(time.monotonic()-start>=budget),1000)
        try:
            db.execute(SQL)
            db.commit()
        except sqlite3.Error:
            db.set_progress_handler(None,0)
            db.rollback()
            atomic_json(state_path,{'attempts':attempts,'retry_after':time.time()+600,'state':'deferred'})
            return False
        atomic_json(state_path,{'attempts':attempts,'state':'ready','duration_seconds':time.monotonic()-start})
        return True
    finally:
        db.close()
