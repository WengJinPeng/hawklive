from __future__ import annotations

import math
from collections.abc import Mapping

# Reading key, alarm metric, threshold value field, enabled field, display label.
PARTICLE_ALARM_CHANNELS = (
    ("pm_0_3_um", "particle_0_3_um", "particle_0_3_max", "particle_0_3_enabled", "0.3 µm"),
    ("pm_0_5_um", "particle_0_5_um", "particle_0_5_max", "particle_0_5_enabled", "0.5 µm"),
    ("pm_1_0_um", "particle_1_0_um", "particle_1_0_max", "particle_1_0_enabled", "1.0 µm"),
    ("pm_2_5_um", "particle_2_5_um", "particle_2_5_max", "particle_2_5_enabled", "2.5 µm"),
    ("pm_5_0_um", "particle_5_0_um", "particle_5_0_max", "particle_5_0_enabled", "5.0 µm"),
    ("pm_10_0_um", "particle_10_0_um", "particle_10_0_max", "particle_10_0_enabled", "10.0 µm"),
)

DEFAULT_THRESHOLDS: dict[str, object] = {
    "profile_name": "Class 100K",
    "particle_0_3_max": None,
    "particle_0_3_enabled": False,
    "particle_0_5_max": 100000.0,
    "particle_0_5_enabled": True,
    "particle_1_0_max": None,
    "particle_1_0_enabled": False,
    "particle_2_5_max": None,
    "particle_2_5_enabled": False,
    "particle_5_0_max": None,
    "particle_5_0_enabled": False,
    "particle_10_0_max": None,
    "particle_10_0_enabled": False,
    "temperature_min": 18.0,
    "temperature_max": 25.0,
    "humidity_min": 40.0,
    "humidity_max": 70.0,
    "alarm_delay_seconds": 300,
}


def _enabled(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalised = value.strip().casefold()
        if normalised in {"1", "true", "yes", "on"}:
            return True
        if normalised in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{field} must be a boolean")


def _finite_number(value: object, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a valid number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def normalise_alarm_thresholds(
    values: Mapping[str, object],
    fallback: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Validate a full or partial alarm policy and return canonical fields.

    Partial payloads use the current policy as a fallback. This lets an older edge
    collector update names or connection details without erasing newer channel rules.
    """
    if not isinstance(values, Mapping):
        raise ValueError("Invalid thresholds")

    base = dict(DEFAULT_THRESHOLDS)
    if fallback:
        base.update({key: value for key, value in fallback.items() if value is not None})
    # Older releases used particle_5_max as the backing field for the 0.5 µm rule.
    if "particle_0_5_max" not in values:
        legacy = values.get("particle_5_max")
        if legacy is None and fallback and "particle_0_5_max" not in fallback:
            legacy = fallback.get("particle_5_max")
        if legacy is not None:
            base["particle_0_5_max"] = legacy

    profile_name = str(values.get("profile_name", base["profile_name"])).strip() or "Custom"
    if len(profile_name) > 100:
        raise ValueError("Alarm policy name must be 100 characters or fewer")
    result: dict[str, object] = {"profile_name": profile_name}

    for _reading_key, _metric, max_field, enabled_field, label in PARTICLE_ALARM_CHANNELS:
        enabled = _enabled(values.get(enabled_field, base[enabled_field]), enabled_field)
        raw_limit = values[max_field] if max_field in values else base.get(max_field)
        limit = None if raw_limit is None or raw_limit == "" else _finite_number(raw_limit, max_field)
        if limit is not None and limit < 0:
            raise ValueError(f"{label} alarm limit cannot be negative")
        if enabled and limit is None:
            raise ValueError(f"{label} alarm limit is required when the rule is enabled")
        result[max_field] = limit
        result[enabled_field] = enabled

    for field in ("temperature_min", "temperature_max", "humidity_min", "humidity_max"):
        result[field] = _finite_number(values.get(field, base[field]), field)
    if float(result["temperature_min"]) >= float(result["temperature_max"]):
        raise ValueError("Temperature minimum must be lower than maximum")
    if float(result["humidity_min"]) >= float(result["humidity_max"]):
        raise ValueError("Humidity minimum must be lower than maximum")

    try:
        delay = int(values.get("alarm_delay_seconds", base["alarm_delay_seconds"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("Alarm delay must be a whole number") from exc
    if not 0 <= delay <= 86400:
        raise ValueError("Alarm delay must be between 0 and 86400 seconds")
    result["alarm_delay_seconds"] = delay
    return result
