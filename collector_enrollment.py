from __future__ import annotations

import json
import os
import platform
import secrets
import socket
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from collector_activation import load_or_create_installation_id
from collector_settings import (
    CollectorSettings,
    load_settings,
    protect_token,
    save_settings,
    unprotect_token,
    validate_settings,
)

ENROLLMENT_FILENAME = "hawkhive-enrollment.json"
ENROLLMENT_SECRET_FILENAME = "collector-enrollment-secret.json"
ENROLLMENT_STATUS_FILENAME = "collector-enrollment-status.json"


@dataclass(frozen=True)
class EnrollmentBundle:
    cloud_url: str
    customer_id: str
    enrollment_token: str


@dataclass(frozen=True)
class EnrollmentState:
    status: str
    settings: CollectorSettings | None = None


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_enrollment_bundle(path: Path) -> EnrollmentBundle:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Enrollment file cannot be read") from exc
    if not isinstance(payload, dict) or int(payload.get("version", 0)) != 1:
        raise ValueError("Enrollment file version is not supported")
    cloud_url = str(payload.get("cloud_url", "")).strip().rstrip("/")
    parsed = urllib.parse.urlparse(cloud_url)
    local_http = parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1", "localhost", "::1",
    }
    if (parsed.scheme != "https" and not local_http) or not parsed.hostname:
        raise ValueError("Enrollment cloud URL must use HTTPS")
    customer_id = str(payload.get("customer_id", "")).strip()
    token = str(payload.get("enrollment_token", "")).strip()
    if not customer_id or len(customer_id) > 200 or len(token) < 32:
        raise ValueError("Enrollment identity is incomplete")
    return EnrollmentBundle(cloud_url, customer_id, token)


def enrollment_candidates(
    explicit: str | None = None,
    *,
    environ: dict[str, str] | None = None,
    executable: str | None = None,
) -> list[Path]:
    values = os.environ if environ is None else environ
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    configured = values.get("DCP_ENROLLMENT_FILE", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    binary = Path(executable or sys.executable)
    candidates.append(binary.resolve().parent / ENROLLMENT_FILENAME)
    candidates.append(Path.cwd() / ENROLLMENT_FILENAME)
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve())
        if key not in seen:
            result.append(candidate)
            seen.add(key)
    return result


def load_or_create_enrollment_secret(settings_path: Path) -> str:
    path = settings_path.with_name(ENROLLMENT_SECRET_FILENAME)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return unprotect_token(
                str(payload.get("protection", "")), str(payload.get("secret", ""))
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Collector enrollment secret cannot be read") from exc
    secret = secrets.token_urlsafe(32)
    protection, protected = protect_token(secret)
    _atomic_text(
        path,
        json.dumps(
            {"version": 1, "protection": protection, "secret": protected},
            separators=(",", ":"),
        ),
    )
    return secret


def request_enrollment(
    bundle: EnrollmentBundle,
    machine_id: str,
    claim_secret: str,
    *,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> EnrollmentState:
    body = json.dumps(
        {
            "customer_id": bundle.customer_id,
            "enrollment_token": bundle.enrollment_token,
            "machine_id": machine_id,
            "claim_secret": claim_secret,
            "hostname": socket.gethostname() or "Windows collector",
            "platform": platform.platform() or "Windows",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{bundle.cloud_url}/api/v1/edge/enroll",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with opener(request, timeout=20) as response:  # type: ignore[attr-defined]
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not payload.get("ok"):
        raise RuntimeError("Cloud rejected collector enrollment")
    status = str(data.get("status", ""))
    if status == "pending":
        return EnrollmentState("pending")
    if status == "approved":
        settings = validate_settings(
            bundle.cloud_url, str(data.get("site_id", "")), str(data.get("token", ""))
        )
        return EnrollmentState("approved", settings=settings)
    raise RuntimeError("Cloud returned an invalid enrollment status")


def enrollment_status(settings_path: Path) -> dict[str, object] | None:
    path = settings_path.with_name(ENROLLMENT_STATUS_FILENAME)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def enroll_if_available(
    settings_path: Path,
    explicit: str | None = None,
    *,
    opener: Callable[..., object] = urllib.request.urlopen,
    environ: dict[str, str] | None = None,
    executable: str | None = None,
) -> EnrollmentState | None:
    if load_settings(settings_path).configured:
        return None
    bundle_path = next(
        (
            path
            for path in enrollment_candidates(
                explicit, environ=environ, executable=executable
            )
            if path.is_file()
        ),
        None,
    )
    if bundle_path is None:
        return None
    machine_id = load_or_create_installation_id(settings_path)
    claim_secret = load_or_create_enrollment_secret(settings_path)
    state = request_enrollment(
        load_enrollment_bundle(bundle_path), machine_id, claim_secret, opener=opener
    )
    status_path = settings_path.with_name(ENROLLMENT_STATUS_FILENAME)
    if state.status == "pending":
        _atomic_text(
            status_path,
            json.dumps({"status": "pending"}, separators=(",", ":")),
        )
        return state
    if state.settings is None:
        raise RuntimeError("Approved enrollment did not include collector settings")
    save_settings(settings_path, state.settings)
    for path in (
        status_path,
        settings_path.with_name(ENROLLMENT_SECRET_FILENAME),
        bundle_path if bundle_path.parent == settings_path.parent else None,
    ):
        if path is None:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return state
