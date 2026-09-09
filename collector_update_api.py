"""Read-only signed release distribution. Contains no customer configuration."""
from __future__ import annotations

import os
import re
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse
from update_protocol import read_json, verify_manifest, verify_executable


def release_directory():
    return Path(os.environ.get('DCP_COLLECTOR_UPDATE_DIR', str(Path(__file__).with_name('release')/'updates')))


def current_release():
    root = release_directory()
    if not (root/'stable.json').exists():
        return None
    envelope = read_json(root/'stable.json')
    payload = verify_manifest(envelope)
    verify_executable(root/(payload['sha256']+'.exe'), payload)
    return envelope


def release_summary():
    try:
        envelope = current_release()
        return {'version': envelope['release']['version']} if envelope else None
    except Exception:
        return None  # An invalid release must never be advertised as installable.


def register_update_routes(app):
    @app.get('/api/v1/collector-updates/stable')
    def stable():
        try:
            envelope = current_release()
        except Exception:
            raise HTTPException(503, 'Collector update is not ready')
        return JSONResponse(envelope or {'available':False}, headers={'Cache-Control':'no-store'})

    @app.get('/api/v1/collector-updates/{artifact}')
    def artifact(artifact: str):
        if not re.fullmatch('[0-9a-f]{64}\\.exe', artifact):
            raise HTTPException(404, 'Update artifact not found')
        # Immutable old artifacts remain downloadable when stable changes during
        # a download. Their digest is checked against the client's signed manifest.
        path = release_directory()/artifact
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, 'Update artifact not found')
        return FileResponse(path, media_type='application/octet-stream',filename='HawkHive-DPC8001-Collector.exe',
                            headers={'Cache-Control':'public, max-age=31536000, immutable'})
