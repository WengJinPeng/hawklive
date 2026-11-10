"""Bounded background maintenance. No sampling, alarm or configuration writes."""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.request
from uuid import UUID

from collector_diagnostics import redact
import maintenance_protocol as protocol
import maintenance_index


class CollectorMaintenance:
    def __init__(self, service, *, trusted_keys=None):
        self.service = service
        self.trusted_keys = trusted_keys
        self.last_report = 0.0
        self.unavailable_until = 0.0
        self.snapshot_cursor = None
        with self.connection(write=True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS maintenance_reading_backups(
                    task_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,site_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL,record_uuid TEXT NOT NULL,raw_sha256 TEXT NOT NULL,
                    original_row_json TEXT NOT NULL,created_at REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'prepared',receipt_json TEXT,completed_at REAL);
            ''')

    @contextmanager
    def connection(self, *, write=False):
        uri = Path(self.service.db_path).resolve().as_uri() + ('?mode=rw' if write else '?mode=ro')
        db = sqlite3.connect(uri, uri=True, timeout=.2)
        db.row_factory = sqlite3.Row
        deadline = time.monotonic() + 1.0
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            with db: yield db
        finally:
            db.close()

    def request(self, path, payload=None):
        body = None if payload is None else protocol.canonical(payload)
        request = urllib.request.Request(self.service.base_url + path, data=body,
                                         headers=self.service._headers(json_body=body is not None))
        return self.service._request_json(request)

    @staticmethod
    def finite(value):
        return value if isinstance(value, (int, float)) and math.isfinite(value) else None

    def snapshot(self):
        with self.connection() as db:
            db.execute('BEGIN')
            source_table = maintenance_index.table(db)
            def query(sql, params):
                try:
                    return db.execute(sql, params)
                except sqlite3.OperationalError:
                    if source_table == 'readings':
                        raise
                    # Same read transaction and progress-handler deadline.
                    return db.execute(sql.replace(source_table, 'readings'), params)
            columns = {row[1] for row in db.execute('PRAGMA table_info(readings)')}
            if 'sync_quarantined_at' not in columns: return {'items': [], 'complete': False, 'total': 0}
            fields = []
            for key in protocol.SOURCE_FIELDS:
                if key not in columns: fields.append('NULL AS ' + key)
                elif key in ('particles_json', 'environment_json', 'alarm_details_json'):
                    fields.append(f'substr({key},1,{protocol.MAX_RAW_BYTES + 1}) AS {key}')
                else: fields.append(key)
            count = query(f"SELECT COUNT(*) FROM {source_table} WHERE customer_id=? AND site_id=? AND source='device' AND sync_quarantined_at IS NOT NULL", (self.service.customer_id, self.service.site_id)).fetchone()[0]
            if count <= protocol.MAX_REPORT_ITEMS: self.snapshot_cursor = None
            params = [self.service.customer_id,self.service.site_id]
            cursor_clause = ''
            if self.snapshot_cursor is not None:
                cursor_clause = ' AND record_uuid>?';params.append(self.snapshot_cursor)
            rows = query('SELECT ' + ','.join(fields) + f",sync_attempts,sync_error,sync_quarantined_at,synced_at FROM {source_table} WHERE customer_id=? AND site_id=? AND source='device' AND sync_quarantined_at IS NOT NULL" + cursor_clause + ' ORDER BY record_uuid LIMIT ?', (*params,protocol.MAX_REPORT_ITEMS + 1)).fetchall()
            self.snapshot_cursor = str(rows[protocol.MAX_REPORT_ITEMS - 1]['record_uuid']) if len(rows) > protocol.MAX_REPORT_ITEMS else None
            rows = rows[:protocol.MAX_REPORT_ITEMS]
        items = []
        for row in rows:
            raw = protocol.source_text(row)
            available = len(raw.encode()) <= protocol.MAX_RAW_BYTES
            items.append({'record_id': str(row['record_uuid'] or ''), 'device_id': str(row['device_id'] or '')[:200],
                          'cleanroom_id': str(row['cleanroom_id'] or '')[:200], 'measured_at': self.finite(row['timestamp']),
                          'attempts': int(row['sync_attempts'] or 0), 'error': redact(row['sync_error'] or '', (self.service.token,))[:500],
                          'quarantined_at': self.finite(row['sync_quarantined_at']), 'synced_at': self.finite(row['synced_at']),
                          'raw_text': raw if available else None,
                          'raw_sha256': protocol.raw_digest(raw) if available else None,
                          'unavailable_reason': None if available else 'Original record exceeds the maintenance size limit'})
        return {'items': items, 'complete': count <= protocol.MAX_REPORT_ITEMS, 'total': count}

    def find(self, db, record_uuid):
        rows = db.execute("SELECT * FROM readings WHERE customer_id=? AND site_id=? AND source='device' AND lower(replace(record_uuid,'-',''))=?", (self.service.customer_id, self.service.site_id, UUID(record_uuid).hex)).fetchall()
        if len(rows) != 1: raise ValueError('Original reading is missing or UUID is ambiguous')
        return rows[0]

    def prepare(self, task):
        if (task['customer_id'],task['site_id'],task['instance_id']) != (self.service.customer_id,self.service.site_id,self.service.instance_id):
            raise ValueError('Maintenance task belongs to another collector')
        with self.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.find(db, task['record_uuid'])
            raw = protocol.source_text(row)
            if protocol.raw_digest(raw) != task['raw_sha256']:
                raise ValueError('Original reading changed; refresh and validate it again')
            backup = db.execute('SELECT * FROM maintenance_reading_backups WHERE task_id=?', (task['task_id'],)).fetchone()
            if backup is not None:
                if (backup['customer_id'], backup['site_id'], backup['instance_id'], backup['raw_sha256']) != (task['customer_id'], task['site_id'], task['instance_id'], task['raw_sha256']):
                    raise ValueError('Maintenance task conflicts with its existing backup')
                return raw
            if row['sync_quarantined_at'] is None or row['synced_at'] is not None:
                raise ValueError('Only an unsynced quarantined reading can be retried')
            db.execute('INSERT INTO maintenance_reading_backups(task_id,customer_id,site_id,instance_id,record_uuid,raw_sha256,original_row_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
                       (task['task_id'],task['customer_id'],task['site_id'],task['instance_id'],task['record_uuid'],task['raw_sha256'],json.dumps(dict(row),ensure_ascii=True,allow_nan=True),time.time()))
        return raw

    def verify(self, envelope, kind):
        return protocol.verify(envelope, customer_id=self.service.customer_id, site_id=self.service.site_id,
                               instance_id=self.service.instance_id, kind=kind, keys=self.trusted_keys)

    def complete(self, envelope):
        receipt = self.verify(envelope, 'receipt')
        with self.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            backup = db.execute('SELECT * FROM maintenance_reading_backups WHERE task_id=?', (receipt['task_id'],)).fetchone()
            if backup is None or (backup['customer_id'],backup['site_id'],backup['instance_id'],backup['record_uuid'],backup['raw_sha256']) != (receipt['customer_id'],receipt['site_id'],receipt['instance_id'],receipt['record_uuid'],receipt['raw_sha256']):
                raise ValueError('Receipt has no matching local backup')
            if backup['state'] != 'completed':
                row = self.find(db, receipt['record_uuid'])
                if protocol.raw_digest(protocol.source_text(row)) != receipt['raw_sha256']:
                    raise ValueError('Reading changed after upload; local queue was preserved')
                if row['sync_quarantined_at'] is None or row['synced_at'] is not None:
                    raise ValueError('Reading queue state changed after maintenance started')
                db.execute('UPDATE readings SET synced_at=?,sync_error=NULL,sync_quarantined_at=NULL WHERE record_uuid=? AND customer_id=? AND site_id=?', (time.time(),row['record_uuid'],receipt['customer_id'],receipt['site_id']))
                db.execute("UPDATE maintenance_reading_backups SET state='completed',receipt_json=?,completed_at=? WHERE task_id=?", (json.dumps(envelope),time.time(),receipt['task_id']))
        self.request('/api/v1/edge/quarantine/tasks/' + receipt['task_id'] + '/ack', {'raw_sha256':receipt['raw_sha256']})

    def once(self):
        if not self.service.customer_id or time.monotonic() < self.unavailable_until: return 0
        try:
            if maintenance_index.enabled(self.service):
                try:
                    maintenance_index.ensure(self.service.db_path)
                except (OSError, ValueError, sqlite3.Error):
                    pass  # Optional optimization cannot disable maintenance reporting.
            if time.monotonic() - self.last_report >= 30 or not self.last_report:
                cursor = self.snapshot_cursor
                try: self.request('/api/v1/edge/quarantine/snapshot', self.snapshot())
                except Exception:
                    self.snapshot_cursor = cursor
                    raise
                self.last_report = time.monotonic()
            response = self.request('/api/v1/edge/quarantine/tasks')
            for job in response.get('data', [])[:1]:
                task = None
                try:
                    if job['state'] == 'accepted':
                        task = self.verify(job['receipt'], 'receipt')
                        self.complete(job['receipt'])
                        return 1
                    task = self.verify(job['task'], 'request')
                    raw = self.prepare(task)
                    response = self.request('/api/v1/edge/quarantine/tasks/' + task['task_id'] + '/execute', {'raw_text':raw})
                    self.complete(response['data']['receipt'])
                    self.service.add_log('INFO','maintenance_reading_completed','Historical reading ' + task['record_uuid'] + ' acknowledged; original backup retained')
                    return 1
                except (ValueError, urllib.error.HTTPError) as error:
                    permanent = isinstance(error, ValueError) or getattr(error,'code',None) in {400,409,422}
                    if task is None or not permanent: raise
                    message = redact(str(error), (self.service.token,))[:500]
                    if isinstance(error, urllib.error.HTTPError):
                        try:
                            detail = json.loads(error.read(2048)).get('detail')
                            if isinstance(detail,str): message = redact('HTTP ' + str(error.code) + ': ' + detail,(self.service.token,))[:500]
                        except (ValueError,OSError): pass
                    self.request('/api/v1/edge/quarantine/tasks/' + task['task_id'] + '/fail', {'raw_sha256':task['raw_sha256'],'error':message})
                    self.service.add_log('ERROR','maintenance_reading_failed',message)
                    return 0
        except urllib.error.HTTPError as error:
            if error.code == 404:
                self.unavailable_until = time.monotonic() + 300
                return 0
            raise
        return 0
