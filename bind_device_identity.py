"""Operator-only binding with tenant/site validation, evidence and revision audit.

Default is a read-only preview. --apply changes configuration, never readings.
Run inside the owning cloud environment; no credentials are accepted as CLI args.
"""
import argparse,json
from uuid import uuid4
from device_identity import normalize_mac

def bind(db, customer, site, device, mac, evidence, apply=False):
    mac=normalize_mac(mac) if mac else None
    if not evidence.strip(): raise ValueError('Reviewed identity evidence is required')
    if not apply: db.execute('SET TRANSACTION READ ONLY')
    # Lock the same site revision used by existing configuration writers.
    lock=' FOR UPDATE' if apply else ''
    if db.execute('SELECT id FROM sites WHERE id=%s AND customer_id=%s AND retired_at IS NULL'+lock,(site,customer)).fetchone() is None:
        raise ValueError('Collector site not found')
    row=db.execute('SELECT name,host FROM devices WHERE id=%s AND customer_id=%s AND site_id=%s AND enabled=true'+lock,(device,customer,site)).fetchone()
    if row is None: raise ValueError('Enabled device not assigned to this customer/site')
    old=db.execute('SELECT expected_mac,required FROM device_identity_bindings WHERE customer_id=%s AND site_id=%s AND device_id=%s',(customer,site,device)).fetchone()
    result=dict(customer_id=customer,site_id=site,device_id=device,name=row[0],host=row[1],before=old,expected_mac=mac,required=True,applied=False)
    if apply:
        db.execute('''INSERT INTO device_identity_bindings(customer_id,site_id,device_id,expected_mac,required,evidence)
            VALUES(%s,%s,%s,%s,true,%s) ON CONFLICT(customer_id,site_id,device_id)
            DO UPDATE SET expected_mac=excluded.expected_mac,required=true,evidence=excluded.evidence,updated_at=now()''',
            (customer,site,device,mac,evidence))
        db.execute('UPDATE sites SET config_version=config_version+1,updated_at=now() WHERE id=%s AND customer_id=%s',(site,customer))
        db.execute('''INSERT INTO configuration_audit_events(id,customer_id,actor_kind,action,target_type,target_id,details)
            VALUES(%s,%s,'operator','device.identity_bound','device',%s,%s::jsonb)''',
            (str(uuid4()),customer,device,json.dumps(dict(before=old,expected_mac=mac,required=True,evidence=evidence))))
        result['applied']=True
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('customer','site','device','evidence'):p.add_argument('--'+name,required=True)
    p.add_argument('--mac',help='Omit to require identity but hold samples pending a reviewed binding')
    p.add_argument('--apply',action='store_true')
    a=p.parse_args()
    from cloud_api import connect
    with connect() as db: print(json.dumps(bind(db,a.customer,a.site,a.device,a.mac,a.evidence,a.apply)))
