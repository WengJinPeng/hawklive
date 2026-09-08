from __future__ import annotations

SUPPORTED_REPORT_LOCALES = {"zh-CN", "en-US"}


def normalize_report_locale(value: object) -> str:
    candidate = str(value or "").strip()
    if candidate in SUPPORTED_REPORT_LOCALES:
        return candidate
    return "zh-CN" if candidate.casefold().startswith("zh") else "en-US"


REPORT_CATALOGS: dict[str, dict[str, object]] = {
    "en-US": {
        "summary_sheet": "Summary",
        "cleanroom_fallback": "Cleanroom",
        "summary_headers": (
            "Cleanroom", "Device", "Metric", "Unit", "Average", "Minimum", "Maximum",
            "Alarm Count", "Record Count", "Data Start Time", "Data End Time",
        ),
        "table_headers": (
            "Timestamp", "Device Name", "0.3 µm (particles/ft³)",
            "0.5 µm (particles/ft³)", "1.0 µm (particles/ft³)",
            "2.5 µm (particles/ft³)", "5.0 µm (particles/ft³)",
            "10.0 µm (particles/ft³)", "Temperature (°C)", "Humidity (%RH)",
            "Alarm Status", "Alarm Details",
        ),
        "csv_headers": (
            "Time", "Device", "0.3 µm", "0.5 µm", "1.0 µm", "Temperature", "Humidity",
        ),
        "particle_unit": "particles/ft³",
    },
    "zh-CN": {
        "summary_sheet": "汇总",
        "cleanroom_fallback": "洁净室",
        "summary_headers": (
            "洁净室", "设备", "指标", "单位", "平均值", "最小值", "最大值",
            "报警次数", "记录数", "数据开始时间", "数据结束时间",
        ),
        "table_headers": (
            "时间", "设备名称", "0.3 µm（个/ft³）", "0.5 µm（个/ft³）",
            "1.0 µm（个/ft³）", "2.5 µm（个/ft³）", "5.0 µm（个/ft³）",
            "10.0 µm（个/ft³）", "温度（°C）", "湿度（%RH）", "报警状态", "报警详情",
        ),
        "csv_headers": ("时间", "设备", "0.3 µm", "0.5 µm", "1.0 µm", "温度", "湿度"),
        "particle_unit": "个/ft³",
    },
}


def report_catalog(locale: object) -> dict[str, object]:
    return REPORT_CATALOGS[normalize_report_locale(locale)]


_PARTICLE_METRICS = {
    "pm_0_3_um": "0.3 µm",
    "pm_0_5_um": "0.5 µm",
    "pm_1_0_um": "1.0 µm",
    "pm_2_5_um": "2.5 µm",
    "pm_5_0_um": "5.0 µm",
    "pm_10_0_um": "10.0 µm",
}


def report_metric_name(metric: object, locale: object) -> str:
    raw = str(metric or "")
    normalized = raw.strip().casefold()
    particle = _PARTICLE_METRICS.get(normalized)
    if particle:
        return particle
    if normalized == "temperature":
        return "温度" if normalize_report_locale(locale) == "zh-CN" else "Temperature"
    if normalized == "humidity":
        return "湿度" if normalize_report_locale(locale) == "zh-CN" else "Humidity"
    return raw


def report_status(status: object, locale: object) -> str:
    raw = str(status or "UNKNOWN")
    normalized = raw.strip().upper()
    if normalize_report_locale(locale) == "zh-CN":
        return {
            "NORMAL": "正常",
            "ALARM_ACTIVE": "报警中",
            "PENDING": "待观察",
            "PENDING_TRIGGER": "待触发",
            "PENDING_CLEAR": "待恢复",
            "OFFLINE": "离线",
            "UNKNOWN": "未知",
        }.get(normalized, raw)
    return {
        "NORMAL": "Normal",
        "ALARM_ACTIVE": "Alarm active",
        "PENDING": "Pending",
        "PENDING_TRIGGER": "Pending trigger",
        "PENDING_CLEAR": "Pending recovery",
        "OFFLINE": "Offline",
        "UNKNOWN": "Unknown",
    }.get(normalized, raw)


def report_limit_description(value: object, locale: object) -> str:
    text = str(value or "")
    if normalize_report_locale(locale) == "zh-CN":
        return text.replace("particles/ft³", "个/ft³")
    return text.replace("个/ft³", "particles/ft³")


def report_alarm_details(details: object, locale: object) -> str:
    if not isinstance(details, list):
        return ""
    rows: list[str] = []
    for detail in details:
        if not isinstance(detail, dict) or detail.get("state") not in {"ALARM_ACTIVE", "PENDING_CLEAR"}:
            continue
        metric = report_metric_name(detail.get("metric"), locale)
        limit = report_limit_description(detail.get("limit"), locale)
        rows.append(f"{metric}: {limit}")
    return "; ".join(rows)


def report_particle_unit(locale: object) -> str:
    return str(report_catalog(locale)["particle_unit"])
