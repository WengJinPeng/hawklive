"""Bounded, signed protocol for isolated historical-reading maintenance."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
from uuid import UUID

PROTOCOL = 1
MAX_RAW_BYTES = 16384
MAX_REPORT_ITEMS = 50
TASK_TTL = 300
KEY_ID = 'hawkhive-maintenance-2026'
# Public verification root only. The signing key remains on the cloud host.
TRUSTED_KEYS: dict[str, str] = {"hawkhive-maintenance-2026":"7d5995bb027719e12229be00478f1bdcb35169d8b3a894bd731bfc4c53cbd3aa"}
SOURCE_FIELDS = (
    'record_uuid', 'customer_id', 'site_id', 'cleanroom_id', 'cleanroom',
    'device_id', 'device', 'timestamp', 'source', 'particles_json',
    'environment_json', 'alarm_status', 'alarm_details_json', 'cleanliness_code',
    'cleanliness_label', 'particle_unit_code', 'particle_unit_label', 'protocol_profile',
)


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def source_text(row) -> str:
    # Nonfinite legacy data remains inspectable as text, never as JSONB/numbers.
    return json.dumps({key: row[key] if key in row.keys() else None for key in SOURCE_FIELDS},
                      sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=True)


def raw_digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def reading_payload(raw_text: str) -> dict:
    if len(raw_text.encode('utf-8')) > MAX_RAW_BYTES:
        raise ValueError('Original record exceeds the maintenance size limit')
    row = json.loads(raw_text)
    if not isinstance(row, dict) or set(row) != set(SOURCE_FIELDS):
        raise ValueError('Original reading fields are incomplete or unexpected')
    payload = {
        'record_uuid': row['record_uuid'], 'customer_id': row['customer_id'],
        'site_id': row['site_id'], 'cleanroom_id': row['cleanroom_id'],
        'cleanroom_name': row['cleanroom'], 'device_id': row['device_id'],
        'device_name': row['device'], 'measured_at': row['timestamp'], 'source': row['source'],
        'particles': json.loads(row['particles_json']),
        'environment': json.loads(row['environment_json']),
        'alarm_status': row['alarm_status'], 'alarm_details': json.loads(row['alarm_details_json']),
        **{key: row[key] for key in ('cleanliness_code', 'cleanliness_label', 'particle_unit_code', 'particle_unit_label', 'protocol_profile')},
    }
    canonical(payload)  # Reject nonfinite data before it can enter PostgreSQL.
    return payload


def signed(payload: dict, private_key, *, key_id=KEY_ID) -> dict:
    return {'payload': payload, 'key_id': key_id,
            'signature': base64.b64encode(private_key.sign(canonical(payload))).decode('ascii')}


def verify(envelope: dict, *, customer_id: str, site_id: str, instance_id: str,
           kind: str, keys=None, now=None) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    keys = TRUSTED_KEYS if keys is None else keys
    now = time.time() if now is None else now
    if not isinstance(envelope, dict) or set(envelope) != {'payload', 'key_id', 'signature'}:
        raise ValueError('Invalid maintenance envelope')
    payload = envelope['payload']
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(keys[envelope['key_id']])).verify(
        base64.b64decode(envelope['signature'], validate=True), canonical(payload))
    expected = {'protocol', 'kind', 'action', 'task_id', 'customer_id', 'site_id', 'instance_id',
                'record_uuid', 'raw_sha256', 'issued_at', 'expires_at'}
    if kind == 'receipt': expected.add('accepted_at')
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError('Unexpected maintenance task fields')
    if payload['protocol'] != PROTOCOL or payload['kind'] != kind or payload['action'] != 'retry_reading':
        raise ValueError('Unsupported maintenance action')
    if (payload['customer_id'], payload['site_id'], payload['instance_id']) != (customer_id, site_id, instance_id):
        raise ValueError('Maintenance task belongs to another collector')
    UUID(payload['task_id'])
    if UUID(payload['record_uuid']).hex != payload['record_uuid']:
        raise ValueError('Noncanonical maintenance record UUID')
    if not re.fullmatch('[0-9a-f]{64}', payload['raw_sha256']):
        raise ValueError('Invalid original-record digest')
    first, last = payload['issued_at'], payload['expires_at']
    if (type(first) is not int or type(last) is not int or not math.isfinite(now)
            or not (first <= now + 60 and now < last) or not 0 < last - first <= TASK_TTL):
        raise ValueError('Maintenance task expired or clock differs')
    if kind == 'receipt' and (type(payload['accepted_at']) is not int or payload['accepted_at'] <= 0):
        raise ValueError('Invalid maintenance receipt')
    return payload
