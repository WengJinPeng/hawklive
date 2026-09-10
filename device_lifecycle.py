"""Tenant-scoped equipment lifecycle and operator-facing audit records."""
from __future__ import annotations

import time
from uuid import uuid4
from typing import Any

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field


class DeviceEdit(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    cleanroom_id: str = Field(min_length=1, max_length=200)
    site_id: str | None = Field(default=None, max_length=200)
    host: str | None = Field(default=None, max_length=64)
    tcp_port: int | None = Field(default=None, ge=1, le=65535)
    slave: int | None = Field(default=None, ge=1, le=247)
    expected_updated_at: str = Field(min_length=1, max_length=64)


class DeviceMaintenance(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    ends_at: float = Field(allow_inf_nan=False)


class DeviceRebind(BaseModel):
    host: str = Field(min_length=1,max_length=64)
    tcp_port: int = Field(default=502,ge=1,le=65535)
    slave: int = Field(default=1,ge=1,le=247)
    expected_updated_at: str = Field(min_length=1,max_length=64)
    duplicate_device_id: str | None = Field(default=None,max_length=200)
    duplicate_updated_at: str | None = Field(default=None,max_length=64)
    confirmed_same_device: bool = False


DEVICE_FIELDS = ('id', 'name', 'cleanroom_id', 'site_id', 'host', 'tcp_port', 'slave', 'enabled', 'updated_at')


def register_device_lifecycle(api: Any) -> None:
    app = api.app

    def locked_device(db, customer_id, device_id):
        db.execute('SELECT 1 FROM customers WHERE id=%s FOR UPDATE', (customer_id,))
        row = db.execute('SELECT id,name,cleanroom_id,site_id,host,tcp_port,slave,enabled,updated_at FROM devices WHERE customer_id=%s AND id=%s FOR UPDATE', (customer_id, device_id)).fetchone()
        if not row:
            raise HTTPException(404, 'Device not found')
        data = dict(zip(DEVICE_FIELDS, row))
        data['updated_at'] = data['updated_at'].isoformat()
        return data

    def audit(db, user, action, device_id, details):
        api.record_configuration_audit(db, str(user['customer_id']), 'customer_user', action,
                                       'device', device_id, details, str(user['id']))

    def validate_target(db, customer_id, device_id, values):
        for table, field, label in [('cleanrooms', 'cleanroom_id', 'Cleanroom'), ('sites', 'site_id', 'Site collector')]:
            active = ' AND retired_at IS NULL' if table == 'sites' else ''
            if not db.execute(f'SELECT 1 FROM {table} WHERE customer_id=%s AND id=%s{active}', (customer_id, values[field])).fetchone():
                raise HTTPException(404, f'{label} not found')
        if db.execute('SELECT 1 FROM devices WHERE customer_id=%s AND enabled AND id<>%s AND cleanroom_id=%s AND lower(name)=lower(%s)', (customer_id, device_id, values['cleanroom_id'], values['name'])).fetchone():
            raise HTTPException(409, 'Device name is already in use in this cleanroom')
        if db.execute('SELECT 1 FROM devices WHERE customer_id=%s AND enabled AND id<>%s AND site_id=%s AND host=%s AND tcp_port=%s AND slave=%s', (customer_id, device_id, values['site_id'], values['host'], values['tcp_port'], values['slave'])).fetchone():
            raise HTTPException(409, 'Device address is already in use; resolve the active device before restoring or saving')

    @app.get('/api/admin/device-lifecycle')
    def lifecycle(user: dict = Depends(api.topology_manager)):
        customer_id = str(user['customer_id'])
        with api.connect() as db:
            rows = db.execute('''SELECT d.id,d.name,d.cleanroom_id,d.site_id,d.host,d.tcp_port,d.slave,d.enabled,d.updated_at,
                extract(epoch FROM d.disabled_at),r.name,s.name,s.config_version,s.applied_config_version,s.config_apply_status,
                s.config_apply_error,extract(epoch FROM s.last_heartbeat_at),m.reason,extract(epoch FROM m.ends_at),
                u.display_name,extract(epoch FROM (SELECT max(l.measured_at) FROM latest_readings l WHERE l.customer_id=d.customer_id AND l.device_id=d.id))
                FROM devices d JOIN cleanrooms r ON r.id=d.cleanroom_id
                LEFT JOIN sites s ON s.id=d.site_id AND s.customer_id=d.customer_id
                LEFT JOIN LATERAL (SELECT reason,ends_at FROM device_maintenance_periods
                    WHERE customer_id=d.customer_id AND device_id=d.id AND started_at<=now()
                      AND LEAST(ends_at,COALESCE(ended_at,ends_at))>now()
                    ORDER BY started_at DESC LIMIT 1) m ON true
                LEFT JOIN LATERAL (SELECT actor_user_id FROM configuration_audit_events WHERE customer_id=d.customer_id
                    AND target_id=d.id AND action='device.deleted' ORDER BY created_at DESC,id DESC LIMIT 1) a ON true
                LEFT JOIN customer_users u ON u.id=a.actor_user_id AND u.customer_id=d.customer_id
                WHERE d.customer_id=%s ORDER BY d.enabled DESC,d.updated_at DESC,d.id''', (customer_id,)).fetchall()
            links = db.execute('SELECT duplicate_device_id,canonical_device_id FROM device_registration_links WHERE customer_id=%s',(customer_id,)).fetchall()
        linked = dict(links)
        devices = []
        for row in rows:
            data = dict(zip(DEVICE_FIELDS, row[:9]))
            data.update(updated_at=row[8].isoformat(), disabled_at=float(row[9]) if row[9] else None,
                        room_name=row[10], site_name=row[11], config_version=row[12], applied_config_version=row[13],
                        sync_state='failed' if row[14] == 'failed' else 'applied' if row[13] is not None and row[13] >= row[12] else 'pending',
                        sync_error=row[15], collector_connected=bool(row[16] and time.time()-float(row[16]) <= api.COLLECTOR_LEASE_SECONDS),
                        maintenance_reason=row[17], maintenance_until=float(row[18]) if row[18] else None,
                        deleted_by=row[19], last_seen_at=float(row[20]) if row[20] else None)
            canonical = linked.get(data['id'],data['id'])
            data['linked_to'] = linked.get(data['id'])
            data['related_device_ids'] = [canonical] + [duplicate for duplicate,kept in links if kept==canonical]
            devices.append(data)
        return {'ok': True, 'data': {'devices': devices, 'can_edit_connections': str(user['role']).lower() in {'admin','customer_admin'}}}

    @app.patch('/api/admin/devices/{device_id}')
    def edit_device(device_id: str, payload: DeviceEdit, user: dict = Depends(api.topology_manager)):
        customer_id = str(user['customer_id'])
        with api.connect() as db:
            before = locked_device(db, customer_id, device_id)
            if not before['enabled']:
                raise HTTPException(409, 'Restore the deleted device before editing')
            if before['updated_at'] != payload.expected_updated_at:
                raise HTTPException(409, 'Device changed in another session; refresh and retry')
            values = {**before, **payload.model_dump(exclude_none=True, exclude={'expected_updated_at'})}
            connection_fields = ('site_id', 'host', 'tcp_port', 'slave')
            if any(values[k] != before[k] for k in connection_fields) and str(user['role']).lower() not in {'admin','customer_admin'}:
                raise HTTPException(403, 'Connection changes require an enterprise administrator')
            values['name'], values['host'], values['tcp_port'], values['slave'] = api.validated_device_connection(values)
            validate_target(db, customer_id, device_id, values)
            changed = {k: values[k] for k in ('name','cleanroom_id',*connection_fields) if values[k] != before[k]}
            if changed:
                db.execute('UPDATE devices SET name=%s,cleanroom_id=%s,site_id=%s,host=%s,tcp_port=%s,slave=%s,updated_at=clock_timestamp() WHERE customer_id=%s AND id=%s',
                           tuple(values[k] for k in ('name','cleanroom_id',*connection_fields)) + (customer_id, device_id))
                db.execute('UPDATE sites SET config_version=config_version+1 WHERE customer_id=%s AND id=ANY(%s)', (customer_id, list({before['site_id'], values['site_id']})))
                audit(db, user, 'device.updated', device_id, {'name':values['name'], 'before':{k:before[k] for k in changed}, 'after':changed})
            config = api.customer_configuration(customer_id, db=db)
        return {'ok':True,'data':config}

    @app.post('/api/admin/devices/{device_id}/restore')
    def restore_device(device_id: str, user: dict = Depends(api.topology_manager)):
        customer_id = str(user['customer_id'])
        with api.connect() as db:
            before = locked_device(db, customer_id, device_id)
            if db.execute('SELECT 1 FROM device_registration_links WHERE customer_id=%s AND duplicate_device_id=%s',(customer_id,device_id)).fetchone():
                raise HTTPException(409, 'This registration is linked to another device and cannot be restored separately')
            if not before['enabled']:
                validate_target(db, customer_id, device_id, before)
                count = db.execute('SELECT count(*) FROM devices WHERE customer_id=%s AND enabled', (customer_id,)).fetchone()[0]
                if count >= api.MAX_ACTIVE_DEVICES:
                    raise HTTPException(409, 'Active device capacity reached')
                db.execute('UPDATE devices SET enabled=true,disabled_at=NULL,updated_at=clock_timestamp() WHERE customer_id=%s AND id=%s', (customer_id, device_id))
                db.execute('UPDATE sites SET config_version=config_version+1 WHERE customer_id=%s AND id=%s', (customer_id, before['site_id']))
                audit(db, user, 'device.restored', device_id, {'name':before['name'], 'history_retained':True})
            config = api.customer_configuration(customer_id, db=db)
        return {'ok':True,'data':config}

    @app.post('/api/admin/devices/{device_id}/rebind')
    def rebind_device(device_id: str, payload: DeviceRebind, user: dict = Depends(api.topology_manager)):
        if str(user['role']).lower() not in {'admin','customer_admin'}:
            raise HTTPException(403,'Connection changes require an enterprise administrator')
        if not payload.confirmed_same_device:
            raise HTTPException(400,'Confirm that both addresses refer to the same physical instrument')
        customer_id=str(user['customer_id'])
        with api.connect() as db:
            before=locked_device(db,customer_id,device_id)
            if not before['enabled']:
                raise HTTPException(409,'Restore the deleted device before editing')
            if db.execute('SELECT 1 FROM device_registration_links WHERE customer_id=%s AND duplicate_device_id=%s',(customer_id,device_id)).fetchone():
                raise HTTPException(409,'Select the original retained device')
            values={**before,**payload.model_dump(include={'host','tcp_port','slave'})}
            values['name'],values['host'],values['tcp_port'],values['slave']=api.validated_device_connection(values)
            duplicate=None
            if payload.duplicate_device_id:
                if payload.duplicate_device_id==device_id:
                    raise HTTPException(400,'The duplicate registration must be a different record')
                duplicate=locked_device(db,customer_id,payload.duplicate_device_id)
                if duplicate['site_id']!=before['site_id']:
                    raise HTTPException(409,'An IP change must stay on the same collector; verify cross-collector moves separately')
                if any(duplicate[k]!=values[k] for k in ('host','tcp_port','slave')):
                    raise HTTPException(409,'New address must match the selected duplicate registration')
                link=db.execute('SELECT canonical_device_id FROM device_registration_links WHERE customer_id=%s AND duplicate_device_id=%s',(customer_id,duplicate['id'])).fetchone()
                if link and link[0]==device_id and not duplicate['enabled'] and all(before[k]==values[k] for k in ('host','tcp_port','slave')):
                    return {'ok':True,'data':api.customer_configuration(customer_id,db=db)}
                if link or db.execute('SELECT 1 FROM device_registration_links WHERE customer_id=%s AND canonical_device_id=%s',(customer_id,duplicate['id'])).fetchone():
                    raise HTTPException(409,'The selected registration already has a device association')
                if not duplicate['enabled'] or duplicate['updated_at']!=payload.duplicate_updated_at:
                    raise HTTPException(409,'Device changed in another session; refresh and retry')
            if not duplicate and all(before[k]==values[k] for k in ('host','tcp_port','slave')):
                return {'ok':True,'data':api.customer_configuration(customer_id,db=db)}
            if before['updated_at']!=payload.expected_updated_at:
                raise HTTPException(409,'Device changed in another session; refresh and retry')
            changed=any(before[k]!=values[k] for k in ('host','tcp_port','slave'))
            if duplicate:
                db.execute('UPDATE devices SET enabled=false,disabled_at=clock_timestamp(),updated_at=clock_timestamp() WHERE customer_id=%s AND id=%s',(customer_id,duplicate['id']))
                db.execute('INSERT INTO device_registration_links(duplicate_device_id,canonical_device_id,customer_id,actor_user_id) VALUES(%s,%s,%s,%s)',(duplicate['id'],device_id,customer_id,str(user['id'])))
                audit(db,user,'device.deleted',duplicate['id'],{'name':duplicate['name'],'history_retained':True,'linked_to':device_id,'reason':'人工确认重复登记'})
            validate_target(db,customer_id,device_id,values)
            if changed:
                db.execute('UPDATE devices SET host=%s,tcp_port=%s,slave=%s,updated_at=clock_timestamp() WHERE customer_id=%s AND id=%s',(values['host'],values['tcp_port'],values['slave'],customer_id,device_id))
            if changed or duplicate:
                db.execute('UPDATE sites SET config_version=config_version+1 WHERE customer_id=%s AND id=%s',(customer_id,before['site_id']))
                audit(db,user,'device.address_changed',device_id,{'name':before['name'], 'before':{k:before[k] for k in ('host','tcp_port','slave')},'after':{k:values[k] for k in ('host','tcp_port','slave')},'duplicate_device_id':duplicate['id'] if duplicate else None,'identity_evidence':'operator_confirmation','history_retained':True})
            config=api.customer_configuration(customer_id,db=db)
        return {'ok':True,'data':config}

    @app.post('/api/admin/devices/{device_id}/maintenance')
    def start_maintenance(device_id: str, payload: DeviceMaintenance, user: dict = Depends(api.topology_manager)):
        reason = payload.reason.strip()
        if not reason or not time.time()+60 <= payload.ends_at <= time.time()+7*86400:
            raise HTTPException(400, 'Provide a reason and an end time between 1 minute and 7 days from now')
        customer_id = str(user['customer_id'])
        with api.connect() as db:
            before = locked_device(db, customer_id, device_id)
            if not before['enabled']:
                raise HTTPException(409, 'Deleted devices cannot enter maintenance')
            if db.execute('SELECT 1 FROM device_maintenance_periods WHERE customer_id=%s AND device_id=%s AND LEAST(ends_at,COALESCE(ended_at,ends_at))>now()', (customer_id,device_id)).fetchone():
                raise HTTPException(409, 'Device is already in maintenance; end it before starting another period')
            db.execute('INSERT INTO device_maintenance_periods(id,customer_id,device_id,reason,ends_at,actor_user_id) VALUES(%s,%s,%s,%s,to_timestamp(%s),%s)', (uuid4(),customer_id,device_id,reason,payload.ends_at,str(user['id'])))
            db.execute("UPDATE email_notifications n SET status='cancelled' FROM alarm_events a WHERE n.event_uuid=a.event_uuid AND n.customer_id=%s AND a.customer_id=n.customer_id AND a.device_id=%s AND n.status='pending'", (customer_id,device_id))
            audit(db,user,'device.maintenance_started',device_id,{'name':before['name'],'reason':reason,'ends_at':payload.ends_at,'paused_notifications':'cloud_email'})
        return {'ok':True,'data':{'active':True}}

    @app.delete('/api/admin/devices/{device_id}/maintenance')
    def end_maintenance(device_id: str, user: dict = Depends(api.topology_manager)):
        customer_id = str(user['customer_id'])
        with api.connect() as db:
            before = locked_device(db,customer_id,device_id)
            rows = db.execute('UPDATE device_maintenance_periods SET ended_at=clock_timestamp() WHERE customer_id=%s AND device_id=%s AND ended_at IS NULL AND ends_at>now() RETURNING reason', (customer_id,device_id)).fetchall()
            if rows:
                audit(db,user,'device.maintenance_ended',device_id,{'name':before['name'],'reason':rows[0][0]})
        return {'ok':True,'data':{'active':False}}

    @app.get('/api/admin/device-audit')
    def device_audit(device_id: str | None = Query(default=None,max_length=200), offset: int = Query(default=0,ge=0), limit: int = Query(default=30,ge=1,le=100), user: dict = Depends(api.topology_manager)):
        customer_id = str(user['customer_id'])
        with api.connect() as db:
            rows = db.execute('''SELECT a.id,a.action,a.target_id,a.details,extract(epoch FROM a.created_at),
                COALESCE(u.display_name,a.actor_kind),count(*) OVER()
                FROM configuration_audit_events a LEFT JOIN customer_users u ON u.id=a.actor_user_id AND u.customer_id=a.customer_id
                WHERE a.customer_id=%s AND a.target_type='device' AND (%s::text IS NULL OR a.target_id=%s)
                ORDER BY a.created_at DESC,a.id DESC LIMIT %s OFFSET %s''', (customer_id,device_id,device_id,limit,offset)).fetchall()
        return {'ok':True,'data':{'items':[{'id':str(r[0]),'action':r[1],'device_id':r[2],'details':r[3],'created_at':float(r[4]),'actor':r[5]} for r in rows], 'total':int(rows[0][6]) if rows else 0}}
