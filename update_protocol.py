"""Signed, bounded release metadata shared by cloud and Windows updater."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

MAX_EXE_BYTES = 100 * 1024 * 1024
UPDATER_PROTOCOL = 1
# Release signing key; the private key is kept outside source and deployment.
TRUSTED_KEYS: dict[str, str] = {"hawkhive-2026": "aea7211db2cb5f8d5571351c36a8c6896bca7aff499db0173e28576d4ff0746c"}


def version_tuple(version: str) -> tuple[int, int, int]:
    if not isinstance(version, str) or not re.fullmatch(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})', version):
        raise ValueError('Invalid collector version')
    return tuple(map(int, version.split('.')))


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def verify_manifest(envelope: dict, *, keys=None, now=None) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    keys = TRUSTED_KEYS if keys is None else keys
    now = time.time() if now is None else now
    payload = envelope['release']
    key = keys[envelope['key_id']]
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(key)).verify(
        base64.b64decode(envelope['signature'], validate=True), canonical(payload))
    version_tuple(payload['version'])
    if payload['platform'] != 'windows-x64' or payload['protocol'] != UPDATER_PROTOCOL:
        raise ValueError('Unsupported updater platform or protocol')
    if type(payload['size']) is not int or not 2 <= payload['size'] <= MAX_EXE_BYTES:
        raise ValueError('Invalid release size')
    if not re.fullmatch('[0-9a-f]{64}', payload['sha256']):
        raise ValueError('Invalid release digest')
    if not (type(payload['issued_at']) is int and type(payload['expires_at']) is int
            and payload['issued_at'] <= now + 300 < payload['expires_at']
            and payload['expires_at'] - payload['issued_at'] <= 90 * 86400):
        raise ValueError('Release manifest expired or invalid')
    return payload


def secure_origin(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('', '/'):
        raise ValueError('Automatic updates require an HTTPS cloud origin')
    return base_url.rstrip('/')


def verify_executable(path: Path, release: dict) -> None:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise ValueError('Update is not a Windows executable')
        stream.seek(0)
        size = 0
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            size += len(chunk)
            if size > release['size']:
                raise ValueError('Update exceeds expected size')
            digest.update(chunk)
    if size != release['size'] or digest.hexdigest() != release['sha256']:
        raise ValueError('Update size or SHA-256 does not match the signed release')


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=True, allow_nan=False)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    temporary.replace(path)


def read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > 65536:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}
