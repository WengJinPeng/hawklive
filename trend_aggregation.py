"""Time-aligned device means, shared by the local and cloud trend APIs."""
from __future__ import annotations

import math

PARTICLE_METRICS = (
    "pm_0_3_um", "pm_0_5_um", "pm_1_0_um", "pm_2_5_um", "pm_5_0_um", "pm_10_0_um",
)
ENVIRONMENT_METRICS = ("temperature", "humidity")


def validate_mean_window(start: float, end: float, bucket_seconds: int) -> None:
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or start >= end:
        raise ValueError("Start and end must be finite, ordered timestamps")
    if not 60 <= bucket_seconds <= 3600:
        raise ValueError("Mean bucket must be between 60 and 3600 seconds")
    if math.floor(end / bucket_seconds) - math.floor(start / bucket_seconds) + 1 > 600:
        raise ValueError("Mean trends support at most 600 time buckets")


def mean_trend_sql(dialect: str, where: str) -> str:
    """Only fixed metric names and server-built scope clauses enter this SQL."""
    if dialect not in {"sqlite", "postgres"}:
        raise ValueError("Unsupported trend database")
    postgres = dialect == "postgres"
    bucket = (
        "FLOOR(EXTRACT(EPOCH FROM measured_at) / %s)::bigint"
        if postgres else "CAST(timestamp / ? AS INTEGER)"
    )
    averages = []
    for group, keys in (("particles", PARTICLE_METRICS), ("environment", ENVIRONMENT_METRICS)):
        for key in keys:
            value = (
                f"CASE WHEN jsonb_typeof({group}->'{key}')='number' "
                f"THEN ({group}->>'{key}')::double precision END"
                if postgres else
                f"CASE WHEN json_type({group}_json, '$.{key}') IN ('integer','real') "
                f"THEN json_extract({group}_json, '$.{key}') END"
            )
            averages.append(f"AVG({value}) AS {key}")
    return f"""
        SELECT {bucket} AS time_bucket, cleanroom_id, device_id,
               MIN(COALESCE(particle_unit_label, '')) AS particle_unit_label,
               COUNT(DISTINCT COALESCE(particle_unit_label, '')) AS particle_unit_count,
               COUNT(*) AS sample_count,
               {', '.join(averages)}
        FROM readings WHERE {where}
        GROUP BY time_bucket, cleanroom_id, device_id
        ORDER BY time_bucket, cleanroom_id, device_id
    """


def mean_trend_reading(row, start: float, end: float, bucket_seconds: int) -> dict:
    bucket, room_id, device_id, unit, unit_count, sample_count, *values = row
    bucket_start = bucket * bucket_seconds
    # Keep the first and last partial buckets inside the requested time range.
    timestamp = (max(start, bucket_start) + min(end, bucket_start + bucket_seconds)) / 2
    return {
        "timestamp": timestamp,
        "bucket_start": bucket_start,
        "bucket_seconds": bucket_seconds,
        "cleanroom_id": room_id,
        "device_id": device_id,
        "particle_unit_label": unit if unit_count == 1 and unit else None,
        "sample_count": sample_count,
        "aggregation": "mean",
        "particles": dict(zip(PARTICLE_METRICS, values[:len(PARTICLE_METRICS)])),
        "environment": dict(zip(ENVIRONMENT_METRICS, values[len(PARTICLE_METRICS):])),
    }
