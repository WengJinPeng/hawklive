from __future__ import annotations

import json
import os
import platform
import socket
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from collector_settings import (
    CollectorSettings,
    load_settings,
    save_settings,
    validate_settings,
)

ACTIVATION_FILENAME = "hawkhive-activation.json"
INSTALLATION_ID_FILENAME = "collector-installation-id"


@dataclass(frozen=True)
class ActivationBundle:
    cloud_url: str
    activation_token: str


def load_activation_bundle(path: Path) -> ActivationBundle:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Activation file cannot be read") from exc
    if not isinstance(payload, dict) or int(payload.get("version", 0)) != 1:
        raise ValueError("Activation file version is not supported")
    cloud_url = str(payload.get("cloud_url", "")).strip().rstrip("/")
    parsed = urllib.parse.urlparse(cloud_url)
    local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (parsed.scheme != "https" and not local_http) or not parsed.hostname:
        raise ValueError("Activation cloud URL must use HTTPS")
    activation_token = str(payload.get("activation_token", "")).strip()
    if len(activation_token) < 24:
        raise ValueError("Activation code is incomplete")
    return ActivationBundle(cloud_url, activation_token)


def activation_candidates(
    explicit: str | None = None,
    *,
    environ: dict[str, str] | None = None,
    executable: str | None = None,
) -> list[Path]:
    values = os.environ if environ is None else environ
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    configured = values.get("DCP_ACTIVATION_FILE", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    binary = Path(executable or sys.executable)
    candidates.append(binary.resolve().parent / ACTIVATION_FILENAME)
    candidates.append(Path.cwd() / ACTIVATION_FILENAME)
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve())
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def claim_activation(
    bundle: ActivationBundle,
    installation_id: str,
    *,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> CollectorSettings:
    body = json.dumps(
        {
            "activation_token": bundle.activation_token,
            "machine_id": installation_id,
            "hostname": socket.gethostname() or "Windows collector",
            "platform": platform.platform() or "Windows",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{bundle.cloud_url}/api/v1/edge/activate",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with opener(request, timeout=20) as response:  # type: ignore[attr-defined]
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not payload.get("ok"):
        raise RuntimeError("Cloud rejected collector activation")
    return validate_settings(
        bundle.cloud_url, str(data.get("site_id", "")), str(data.get("token", ""))
    )


def load_or_create_installation_id(settings_path: Path) -> str:
    """Persist identity before claiming so a lost activation response is retryable."""
    path = settings_path.with_name(INSTALLATION_ID_FILENAME)
    try:
        value = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        value = ""
    except OSError as exc:
        raise RuntimeError("Collector installation identity cannot be read") from exc
    if len(value) == 32 and all(character in "0123456789abcdef" for character in value):
        return value
    if value:
        raise RuntimeError("Collector installation identity is invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = uuid4().hex
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return load_or_create_installation_id(settings_path)
    except OSError as exc:
        raise RuntimeError("Collector installation identity cannot be stored") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="") as stream:
            stream.write(candidate + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        try:
            path.unlink()
        except OSError:
            pass
        raise RuntimeError("Collector installation identity cannot be stored") from exc
    return candidate


def activate_if_available(
    settings_path: Path,
    explicit: str | None = None,
    *,
    opener: Callable[..., object] = urllib.request.urlopen,
    environ: dict[str, str] | None = None,
    executable: str | None = None,
) -> CollectorSettings | None:
    current = load_settings(settings_path)
    if current.configured:
        return None
    activation_path = next(
        (
            path
            for path in activation_candidates(
                explicit, environ=environ, executable=executable
            )
            if path.is_file()
        ),
        None,
    )
    if activation_path is None:
        return None
    installation_id = load_or_create_installation_id(settings_path)
    settings = claim_activation(
        load_activation_bundle(activation_path), installation_id, opener=opener
    )
    save_settings(settings_path, settings)
    try:
        activation_path.unlink()
    except OSError:
        # The claim is already consumed and durable settings are safely stored.
        # A locked removable-media file must not prevent the collector starting.
        pass
    return settings
