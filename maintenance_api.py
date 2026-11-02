"""Tenant/collector-bound quarantine inspection, validation and exact UUID retry."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from collector_diagnostics import redact
import maintenance_protocol as protocol


class SnapshotItem(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    record_id: str = Field(max_length=200)
    device_id: str = Field(max_length=200)
    cleanroom_id: str = Field(max_length=200)
    measured_at: float | None
    attempts: int = Field(ge=0, le=1000000000)
    error: str = Field(max_length=500)
    quarantined_at: float | None
    synced_at: float | None
    raw_text: str | None = Field(max_length=protocol.MAX_RAW_BYTES)
    raw_sha256: str | None = Field(pattern='^[0-9a-f]{64}$')
    unavailable_reason: str | None = Field(max_length=200)

    @model_validator(mode='after')
    def check_digest(self):
        if self.raw_text is not None:
            if len(self.raw_text.encode()) > protocol.MAX_RAW_BYTES or protocol.raw_digest(self.raw_text) != self.raw_sha256:
                raise ValueError('Original reading digest or size is invalid')
        elif self.raw_sha256 is not None:
            raise ValueError('Digest requires original reading')
        return self


class Snapshot(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[SnapshotItem] = Field(max_length=protocol.MAX_REPORT_ITEMS)
    complete: bool
    total: int = Field(ge=0, le=1000000000)

    @model_validator(mode='after')
    def check_count(self):
        if self.total < len(self.items) or (self.complete and self.total != len(self.items)):
            raise ValueError('Snapshot count is inconsistent')
        if len({item.record_id for item in self.items}) != len(self.items):
            raise ValueError('Snapshot contains duplicate record identifiers')
        return self


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    validation_id: UUID
    raw_sha256: str = Field(pattern='^[0-9a-f]{64}$')
    record_uuid: UUID


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    raw_text: str = Field(max_length=protocol.MAX_RAW_BYTES)


class AckRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    raw_sha256: str = Field(pattern='^[0-9a-f]{64}$')


class FailureRequest(AckRequest):
    error: str = Field(min_length=1, max_length=500)


def signing_key():
    configured = os.environ.get('DCP_MAINTENANCE_SIGNING_KEY_FILE', '')
    if not configured:
        raise HTTPException(503, 'Collector maintenance signing is not configured')
    try:
        key = serialization.load_pem_private_key(Path(configured).read_bytes(), password=None)
        public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        if not isinstance(key, Ed25519PrivateKey) or protocol.TRUSTED_KEYS.get(protocol.KEY_ID) != public:
            raise ValueError('Key does not match the collector trust root')
        return key
    except (OSError, ValueError, TypeError):
        raise HTTPException(503, 'Collector maintenance signing is unavailable') from None


def signing_available():
    try: signing_key(); return True
    except HTTPException: return False


def record_result(row):
    return {'id': str(row[0]), 'instance_id': row[1], 'record_id': row[2], 'device_id': row[3],
            'cleanroom_id': row[4], 'raw_text': row[5], 'raw_sha256': row[6],
            'metadata': row[7], 'validation': row[8], 'present': row[9], 'reported_at': row[10].timestamp()}


RECORD_COLUMNS = 'id,instance_id,record_id,device_id,cleanroom_id,raw_text,raw_sha256,metadata,validation,present,reported_at'
TASK_COLUMNS = 'id,instance_id,record_uuid,raw_sha256,state,created_at,expires_at,accepted_at'


def envelope(row, identity, kind):
    now = int(time.time())
    first = int(row[5].timestamp()) if kind == 'request' else now
    last = int(row[6].timestamp()) if kind == 'request' else now + protocol.TASK_TTL
    payload = {'protocol':protocol.PROTOCOL,'kind':kind,'action':'retry_reading','task_id':str(row[0]),
               'customer_id':identity['customer_id'],'site_id':identity['site_id'],'instance_id':row[1],
               'record_uuid':row[2],'raw_sha256':row[3],'issued_at':first,'expires_at':last}
    if kind == 'receipt': payload['accepted_at'] = int(row[7].timestamp())
    return protocol.signed(payload, signing_key())


def validate_original(api, db, raw_text, identity):
    try:
        payload = protocol.reading_payload(raw_text)
        item = api.ReadingIn.model_validate(payload)
        if item.customer_id != identity['customer_id'] or item.site_id != identity['site_id']:
            raise HTTPException(403, 'Original reading belongs to another customer or collector')
        api.authorize_ingest_target(db, identity, item.cleanroom_id, item.device_id, historical_at=item.measured_at)
        row = db.execute('''SELECT customer_id,site_id,cleanroom_id,device_id,extract(epoch FROM measured_at),source,
            particles,environment,alarm_status,alarm_details,cleanliness_code,cleanliness_label,
            particle_unit_code,particle_unit_label,protocol_profile FROM readings WHERE record_uuid=%s::uuid''', (str(item.record_uuid),)).fetchone()
        if row is not None:
            expected = (identity['customer_id'],identity['site_id'],item.cleanroom_id,item.device_id)
            if tuple(row[:4]) != expected:
                raise HTTPException(409, 'Record UUID already belongs to another target')
            values = (item.source,item.particles,item.environment,item.alarm_status,item.alarm_details,item.cleanliness_code,item.cleanliness_label,item.particle_unit_code,item.particle_unit_label,item.protocol_profile)
            if abs(float(row[4]) - item.measured_at) > .0000011 or tuple(row[5:]) != values:
                raise HTTPException(409, 'Record UUID already contains different data; original was preserved')
        return {'valid':True,'issues':[],'already_in_cloud':row is not None}, item
    except ValidationError as error:
        issues = [{'field':'.'.join(map(str,item['loc'])) or 'original_record','message':item['msg'][:300]} for item in error.errors(include_input=False,include_url=False)[:20]]
    except HTTPException as error:
        issues = [{'field':'ownership_or_history','message':str(error.detail)[:300]}]
    except (ValueError, TypeError, KeyError):
        issues = [{'field':'original_record','message':'Original fields, JSON, UUID or numeric values are invalid'}]
    return {'valid':False,'issues':issues,'already_in_cloud':False}, None


def register_maintenance(api):
    app = api.app

    def site_row(db, site_id, customer_id, *, lock=False):
        row = db.execute('SELECT active_collector_instance,collector_status,last_heartbeat_at,retired_at FROM sites WHERE id=%s AND customer_id=%s' + (' FOR UPDATE' if lock else ''), (site_id,customer_id)).fetchone()
        if row is None: raise HTTPException(404,'Collector not found')
        return row

    def load_record(db, entry_id, site_id, customer_id):
        row = db.execute('SELECT '+RECORD_COLUMNS+' FROM collector_quarantine_records WHERE id=%s AND customer_id=%s AND site_id=%s', (entry_id,customer_id,site_id)).fetchone()
        if row is None: raise HTTPException(404,'Quarantined reading not found')
        return record_result(row)

    def usable(record, site):
        if (not record['present'] or record['instance_id'] != site[0] or site[3] is not None
                or not record['raw_text'] or time.time()-record['reported_at'] > protocol.TASK_TTL
                or record['metadata'].get('synced_at') is not None):
            raise HTTPException(409,'Refresh the collector quarantine snapshot before proceeding')

    def writer(user=Depends(api.topology_manager)):
        if str(user.get('role','')).lower() not in {'admin','customer_admin'}:
            raise HTTPException(403,'Administrator permission is required for reading replay')
        return user

    @app.post('/api/v1/edge/quarantine/snapshot')
    def snapshot(payload: Snapshot, identity=Depends(api.edge_identity)):
        for item in payload.items:
            if item.raw_text is not None:
                try: raw = json.loads(item.raw_text)
                except ValueError: raise HTTPException(400,'Malformed original source') from None
                if not isinstance(raw,dict) or set(raw) != set(protocol.SOURCE_FIELDS):
                    raise HTTPException(400,'Unexpected original source fields')
                if (raw.get('customer_id'),raw.get('site_id'),str(raw.get('record_uuid') or '')) != (identity['customer_id'],identity['site_id'],item.record_id):
                    raise HTTPException(403,'Snapshot original belongs to another scope')
        with api.connect() as db:
            db.execute("SET LOCAL statement_timeout='3s'")
            site_row(db,identity['site_id'],identity['customer_id'],lock=True)
            for item in payload.items:
                metadata = item.model_dump(exclude={'raw_text','raw_sha256'})
                metadata['error'] = redact(metadata['error'])
                db.execute('''INSERT INTO collector_quarantine_records(id,customer_id,site_id,instance_id,record_id,device_id,cleanroom_id,raw_text,raw_sha256,metadata)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT(customer_id,site_id,instance_id,record_id) DO UPDATE SET
                    device_id=excluded.device_id,cleanroom_id=excluded.cleanroom_id,raw_text=excluded.raw_text,raw_sha256=excluded.raw_sha256,
                    metadata=excluded.metadata,present=true,reported_at=now(),
                    validation=CASE WHEN collector_quarantine_records.raw_sha256 IS NOT DISTINCT FROM excluded.raw_sha256 THEN collector_quarantine_records.validation ELSE NULL END''',
                    (str(uuid4()),identity['customer_id'],identity['site_id'],identity['instance_id'],item.record_id,item.device_id,item.cleanroom_id,item.raw_text,item.raw_sha256,json.dumps(metadata)))
            if payload.complete:
                db.execute('UPDATE collector_quarantine_records SET present=false,validation=NULL WHERE customer_id=%s AND site_id=%s AND instance_id=%s AND NOT(record_id=ANY(%s))', (identity['customer_id'],identity['site_id'],identity['instance_id'],[item.record_id for item in payload.items]))
            db.execute('''INSERT INTO collector_quarantine_reports(site_id,customer_id,instance_id,total,complete) VALUES(%s,%s,%s,%s,%s)
                ON CONFLICT(site_id) DO UPDATE SET instance_id=excluded.instance_id,total=excluded.total,complete=excluded.complete,reported_at=now()''', (identity['site_id'],identity['customer_id'],identity['instance_id'],payload.total,payload.complete))
        return {'ok':True}

    @app.get('/api/admin/sites/{site_id}/quarantine')
    def listing(site_id: str, after: str | None = Query(None,max_length=200), limit: int = Query(50,ge=1,le=200), user=Depends(api.topology_manager)):
        customer = str(user['customer_id'])
        with api.connect() as db:
            site = site_row(db,site_id,customer)
            params = [customer,site_id,site[0]]
            clause = ''
            if after is not None: clause = ' AND record_id>%s';params.append(after)
            rows = db.execute('SELECT '+RECORD_COLUMNS+' FROM collector_quarantine_records WHERE customer_id=%s AND site_id=%s AND instance_id=%s AND present'+clause+' ORDER BY record_id LIMIT %s', (*params,limit+1)).fetchall()
            report = db.execute('SELECT total,complete,reported_at FROM collector_quarantine_reports WHERE customer_id=%s AND site_id=%s AND instance_id=%s', (customer,site_id,site[0])).fetchone()
            jobs = db.execute('SELECT id,record_uuid,state,created_at,accepted_at,completed_at,error FROM collector_quarantine_tasks WHERE customer_id=%s AND site_id=%s ORDER BY created_at DESC LIMIT 20', (customer,site_id)).fetchall()
        supported = (site[1] or {}).get('maintenance_protocol') == protocol.PROTOCOL
        permitted = str(user.get('role','')).lower() in {'admin','customer_admin'}
        online = bool(site[3] is None and site[2] and time.time()-site[2].timestamp() <= api.COLLECTOR_LEASE_SECONDS)
        signer = signing_available()
        return {'ok':True,'data':{'items':[record_result(row) for row in rows[:limit]],
                'next_after':rows[limit-1][2] if len(rows)>limit else None,
                'report':None if report is None else {'total':report[0],'complete':report[1],'reported_at':report[2].timestamp()},
                'supported':supported,'can_retry':permitted and signer and supported and online,
                'retry_unavailable_reason': 'admin_required' if not permitted else 'collector_unavailable' if not online else 'signing_unavailable' if not signer else None,
                'jobs':[{'id':str(row[0]),'record_uuid':row[1],'state':row[2],'created_at':row[3].timestamp(),'accepted_at':row[4].timestamp() if row[4] else None,'completed_at':row[5].timestamp() if row[5] else None,'error':row[6]} for row in jobs]}}

    @app.post('/api/admin/sites/{site_id}/quarantine/{entry_id}/validate')
    def validate(site_id: str, entry_id: UUID, user=Depends(api.topology_manager)):
        customer = str(user['customer_id'])
        with api.connect() as db:
            db.execute('SET TRANSACTION READ ONLY');db.execute("SET LOCAL statement_timeout='3s'")
            site = site_row(db,site_id,customer)
            record = load_record(db,entry_id,site_id,customer);usable(record,site)
            result,_ = validate_original(api,db,record['raw_text'],{'customer_id':customer,'site_id':site_id})
        result.update(id=str(uuid4()),raw_sha256=record['raw_sha256'],validated_at=time.time(),expires_at=time.time()+protocol.TASK_TTL)
        with api.connect() as db:
            updated = db.execute('UPDATE collector_quarantine_records SET validation=%s::jsonb WHERE id=%s AND customer_id=%s AND site_id=%s AND raw_sha256=%s AND present', (json.dumps(result),entry_id,customer,site_id,record['raw_sha256']))
            if updated.rowcount != 1: raise HTTPException(409,'Original snapshot changed during validation')
        return {'ok':True,'data':result}

    @app.post('/api/admin/sites/{site_id}/quarantine/{entry_id}/retry')
    def retry(site_id: str, entry_id: UUID, payload: RetryRequest, request: Request, user=Depends(writer)):
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403,'Maintenance origin does not match')
        signing_key()
        customer = str(user['customer_id'])
        with api.connect() as db:
            db.execute("SET LOCAL statement_timeout='3s'")
            site = site_row(db,site_id,customer,lock=True)
            old = db.execute('SELECT id,record_entry_id,raw_sha256,record_uuid,state FROM collector_quarantine_tasks WHERE customer_id=%s AND site_id=%s AND request_id=%s', (customer,site_id,payload.request_id)).fetchone()
            if old is not None:
                if (str(old[1]),old[2],old[3]) != (str(entry_id),payload.raw_sha256,payload.record_uuid.hex):
                    raise HTTPException(409,'Request identifier was used for another original record')
                return {'ok':True,'data':{'id':str(old[0]),'state':old[4]}}
            record = load_record(db,entry_id,site_id,customer);usable(record,site)
            validation = record['validation'] or {}
            if (not validation.get('valid') or validation.get('id') != str(payload.validation_id)
                    or validation.get('expires_at',0) <= time.time() or record['raw_sha256'] != payload.raw_sha256
                    or UUID(record['record_id']).hex != payload.record_uuid.hex):
                raise HTTPException(409,'Validate this exact original reading again before retrying')
            if ((site[1] or {}).get('maintenance_protocol') != protocol.PROTOCOL or not site[2]
                    or time.time()-site[2].timestamp() > api.COLLECTOR_LEASE_SECONDS):
                raise HTTPException(409,'Collector maintenance is unsupported or offline')
            result,_ = validate_original(api,db,record['raw_text'],{'customer_id':customer,'site_id':site_id})
            if not result['valid']: raise HTTPException(409,'Original no longer passes validation; refresh validation')
            db.execute("UPDATE collector_quarantine_tasks SET state='expired' WHERE customer_id=%s AND site_id=%s AND state='pending' AND expires_at<=now()", (customer,site_id))
            if db.execute("SELECT 1 FROM collector_quarantine_tasks WHERE customer_id=%s AND site_id=%s AND state IN ('pending','accepted')", (customer,site_id)).fetchone():
                raise HTTPException(409,'Another maintenance reading is pending acknowledgement')
            task_id = str(uuid4())
            db.execute("INSERT INTO collector_quarantine_tasks(id,customer_id,site_id,instance_id,record_entry_id,record_uuid,raw_sha256,request_id,actor_user_id,state,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',now()+interval '300 seconds')", (task_id,customer,site_id,site[0],entry_id,payload.record_uuid.hex,payload.raw_sha256,payload.request_id,user['id']))
            api.record_configuration_audit(db,customer,'customer_user','quarantine.retry_requested','collector',site_id,{'task_id':task_id,'record_uuid':payload.record_uuid.hex,'raw_sha256':payload.raw_sha256},str(user['id']))
        return {'ok':True,'data':{'id':task_id,'state':'pending'}}

    @app.get('/api/v1/edge/quarantine/tasks')
    def tasks(identity=Depends(api.edge_identity)):
        with api.connect() as db:
            db.execute("UPDATE collector_quarantine_tasks SET state='expired' WHERE customer_id=%s AND site_id=%s AND instance_id=%s AND state='pending' AND expires_at<=now()", (identity['customer_id'],identity['site_id'],identity['instance_id']))
            rows = db.execute('SELECT '+TASK_COLUMNS+" FROM collector_quarantine_tasks WHERE customer_id=%s AND site_id=%s AND instance_id=%s AND state IN ('pending','accepted') ORDER BY created_at LIMIT 1", (identity['customer_id'],identity['site_id'],identity['instance_id'])).fetchall()
        return {'ok':True,'data':[{'state':row[4], 'receipt' if row[4]=='accepted' else 'task':envelope(row,identity,'receipt' if row[4]=='accepted' else 'request')} for row in rows]}

    @app.post('/api/v1/edge/quarantine/tasks/{task_id}/execute')
    def execute(task_id: UUID, payload: ExecuteRequest, identity=Depends(api.edge_identity)):
        if len(payload.raw_text.encode()) > protocol.MAX_RAW_BYTES: raise HTTPException(413,'Original reading is too large')
        with api.connect() as db:
            db.execute("SET LOCAL statement_timeout='3s'")
            db.execute('SELECT 1 FROM customers WHERE id=%s FOR KEY SHARE', (identity['customer_id'],))
            row = db.execute('SELECT '+TASK_COLUMNS+' FROM collector_quarantine_tasks WHERE id=%s AND customer_id=%s AND site_id=%s AND instance_id=%s FOR UPDATE', (task_id,identity['customer_id'],identity['site_id'],identity['instance_id'])).fetchone()
            if row is None: raise HTTPException(404,'Maintenance task not found')
            if protocol.raw_digest(payload.raw_text) != row[3]: raise HTTPException(409,'Original reading differs from the approved snapshot')
            if row[4] in ('accepted','completed'):
                return {'ok':True,'data':{'receipt':envelope(row,identity,'receipt')}}
            if row[4] != 'pending' or row[6].timestamp() <= time.time(): raise HTTPException(409,'Maintenance task has expired')
            result,item = validate_original(api,db,payload.raw_text,identity)
            if not result['valid'] or item.record_uuid.hex != row[2]: raise HTTPException(409,'Original reading no longer passes cloud validation')
            acknowledgement = api.ingest_batch(api.ReadingBatch(site_id=identity['site_id'],readings=[item]), identity=identity)
            if {UUID(str(value)).hex for value in acknowledgement.get('accepted',[])} != {row[2]}:
                raise HTTPException(502,'Cloud did not acknowledge the exact original UUID')
            verified,_ = validate_original(api,db,payload.raw_text,identity)
            if not verified['valid'] or not verified['already_in_cloud']:
                raise HTTPException(409,'Cloud UUID changed concurrently; local queue must be preserved')
            row = db.execute("UPDATE collector_quarantine_tasks SET state='accepted',accepted_at=now() WHERE id=%s RETURNING "+TASK_COLUMNS, (task_id,)).fetchone()
        return {'ok':True,'data':{'receipt':envelope(row,identity,'receipt')}}

    @app.post('/api/v1/edge/quarantine/tasks/{task_id}/ack')
    def acknowledge(task_id: UUID, payload: AckRequest, identity=Depends(api.edge_identity)):
        with api.connect() as db:
            row = db.execute('SELECT raw_sha256,state,record_entry_id FROM collector_quarantine_tasks WHERE id=%s AND customer_id=%s AND site_id=%s AND instance_id=%s FOR UPDATE', (task_id,identity['customer_id'],identity['site_id'],identity['instance_id'])).fetchone()
            if row is None: raise HTTPException(404,'Maintenance task not found')
            if row[0] != payload.raw_sha256 or row[1] not in ('accepted','completed'):
                raise HTTPException(409,'Maintenance task has no matching accepted receipt')
            db.execute("UPDATE collector_quarantine_tasks SET state='completed',completed_at=COALESCE(completed_at,now()) WHERE id=%s", (task_id,))
            db.execute('UPDATE collector_quarantine_records SET present=false,validation=NULL WHERE id=%s', (row[2],))
        return {'ok':True}

    @app.post('/api/v1/edge/quarantine/tasks/{task_id}/fail')
    def failed(task_id: UUID, payload: FailureRequest, identity=Depends(api.edge_identity)):
        with api.connect() as db:
            row = db.execute('SELECT raw_sha256,state FROM collector_quarantine_tasks WHERE id=%s AND customer_id=%s AND site_id=%s AND instance_id=%s FOR UPDATE', (task_id,identity['customer_id'],identity['site_id'],identity['instance_id'])).fetchone()
            if row is None: raise HTTPException(404,'Maintenance task not found')
            if row[0] != payload.raw_sha256: raise HTTPException(409,'Maintenance failure digest differs')
            if row[1] in ('pending','accepted'):
                db.execute("UPDATE collector_quarantine_tasks SET state=CASE WHEN state='pending' AND expires_at<=now() THEN 'expired' ELSE 'failed' END,error=%s,failed_at=now() WHERE id=%s", (redact(payload.error),task_id))
        return {'ok':True}
