from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

PROTECTION_DPAPI_MACHINE = "windows-dpapi-machine-v1"
PROTECTION_PLAINTEXT = "plaintext-v1"


SITE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,99}$")


class CollectorSettingsError(ValueError):
    pass


@dataclass(frozen=True)
class CollectorSettings:
    cloud_url: str
    site_id: str
    token: str

    @property
    def configured(self) -> bool:
        return bool(self.cloud_url and self.site_id and self.token)

    def public_dict(self) -> dict[str, object]:
        return {
            "cloud_url": self.cloud_url,
            "site_id": self.site_id,
            "token_configured": bool(self.token),
        }


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(value: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(value)
    return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi_transform(value: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise OSError("Windows DPAPI is available only on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    input_blob, input_buffer = _blob(value)
    output_blob = _DataBlob()
    # CRYPTPROTECT_LOCAL_MACHINE lets the Windows service decrypt after reboot;
    # CRYPTPROTECT_UI_FORBIDDEN prevents a service from hanging on a desktop prompt.
    flags = 0x1 if decrypt else 0x1 | 0x4
    if decrypt:
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_int
        success = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob), None, None, None, None, flags, ctypes.byref(output_blob)
        )
    else:
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int
        success = crypt32.CryptProtectData(
            ctypes.byref(input_blob), ctypes.c_wchar_p("HawkHive DCP8001 edge token"), None, None, None,
            flags, ctypes.byref(output_blob),
        )
    _ = input_buffer  # Keep the input buffer alive for the native call.
    if not success:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(output_blob.pbData)


def protect_token(token: str) -> tuple[str, str]:
    if os.name == "nt":
        protected = _dpapi_transform(token.encode("utf-8"), decrypt=False)
        return PROTECTION_DPAPI_MACHINE, base64.b64encode(protected).decode("ascii")
    return PROTECTION_PLAINTEXT, token


def unprotect_token(protection: str, value: str) -> str:
    if protection == PROTECTION_DPAPI_MACHINE:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
        return _dpapi_transform(raw, decrypt=True).decode("utf-8")
    if protection in {"", PROTECTION_PLAINTEXT}:
        return value
    raise ValueError("Unsupported collector token protection scheme")


def validate_settings(cloud_url: str, site_id: str, token: str) -> CollectorSettings:
    url = cloud_url.strip().rstrip("/")
    site = site_id.strip()
    secret = token.strip()
    parsed = urllib.parse.urlparse(url)
    local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not local_http:
        raise ValueError("Cloud URL must use HTTPS")
    if not parsed.hostname:
        raise ValueError("Cloud URL is invalid")
    if not SITE_ID_PATTERN.fullmatch(site):
        raise ValueError("Site ID may contain letters, numbers, dots, dashes, and underscores")
    if len(secret) < 24:
        raise ValueError("Edge Token is too short")
    return CollectorSettings(url, site, secret)


def load_settings(path: Path) -> CollectorSettings:
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            protection = str(payload.get("token_protection", ""))
            token_value = str(payload.get("protected_token", payload.get("token", "")))
            settings = validate_settings(
                str(payload.get("cloud_url", "")),
                str(payload.get("site_id", "")),
                unprotect_token(protection, token_value),
            )
            # Transparently remove legacy plaintext tokens when first read on Windows.
            if os.name == "nt" and "token" in payload:
                save_settings(path, settings)
            return settings
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise CollectorSettingsError(
                f"Collector settings cannot be read safely: {path}"
            ) from exc
    cloud_url = os.environ.get("DCP_CLOUD_URL", "").strip()
    site_id = os.environ.get("DCP_SITE_ID", "").strip()
    token = os.environ.get("DCP_CLOUD_TOKEN", "").strip()
    if cloud_url and site_id and token:
        return validate_settings(cloud_url, site_id, token)
    return CollectorSettings("", "", "")


def save_settings(path: Path, settings: CollectorSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    protection, protected_token = protect_token(settings.token)
    payload = json.dumps(
        {
            "version": 2,
            "cloud_url": settings.cloud_url,
            "site_id": settings.site_id,
            "token_protection": protection,
            "protected_token": protected_token,
        },
        separators=(",", ":"),
    )
    handle, temporary = tempfile.mkstemp(prefix="collector-settings-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
