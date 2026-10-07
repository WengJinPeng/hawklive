"""Small, secret-free startup evidence for unattended Windows installations."""
from __future__ import annotations

import math
import shutil
import ssl
import time
import urllib.error
from pathlib import Path

from update_protocol import atomic_json, read_json

STATUS_FILE = "collector-startup-status.json"


def record_startup(data_dir: Path, state: str) -> None:
    # Diagnostics must never prevent collection, including on a full disk.
    try:
        atomic_json(data_dir / STATUS_FILE, {"state": state, "updated_at": time.time()})
    except OSError:
        pass


def registration_failure(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403, 410):
            return "registration_rejected"
        return "cloud_unavailable"
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ssl.SSLError):
        return "tls_failed"
    if isinstance(reason, (OSError, TimeoutError)):
        return "network_failed"
    return "registration_failed"


def fresh_startup(data_dir: Path, since: float) -> dict:
    try:
        status = read_json(data_dir / STATUS_FILE)
        if math.isfinite(float(status.get("updated_at", 0))) and since <= float(status.get("updated_at", 0)) <= time.time() + 5:
            return status
    except (OSError, ValueError, TypeError):
        pass
    return {}


def startup_message(data_dir: Path, since: float) -> str:
    state = fresh_startup(data_dir, since).get("state")
    messages = {
        "pending": "本机已提交登记，正在等待管理员批准。请联系管理员在云端批准本机，无需重新安装。",
        "network_failed": "本机连接云端失败。请检查网线或 Wi-Fi，并确认这台电脑能打开云端网站。恢复连接后会自动重试，无需重新安装。",
        "tls_failed": "本机与云端的安全连接失败。请先校准电脑日期和时间；仍未恢复时，将此提示发给管理员检查证书。",
        "registration_rejected": "云端未接受本机的登记凭据。请联系管理员获取新的安装包，勿反复安装同一份包。",
        "cloud_unavailable": "云端登记服务暂时不可用。系统会自动重试；持续出现时，请将此提示发给管理员。",
        "registration_failed": "本机登记未完成，暂时无法确定原因。请将此提示发给管理员，系统会自动重试。",
        "starting": "后台程序仍在启动，暂时未检测到本机服务就绪。请保持电脑开机，稍后查看云端是否上线。",
        "ready": "本机服务曾启动，但当前未通过检查。请将此提示发给管理员核查后台运行状态。",
        "storage_full": "磁盘已满，采集程序无法正常写入。请清理回收站或无关文件，不要删除 HawkHive 数据；留出至少 1 GB 后等待自动重试。",
        "permission_failed": "采集程序无法访问运行所需的文件。请将此提示发给管理员检查目录权限或安全软件的拦截记录。",
        "port_in_use": "本机采集服务需要的端口已被占用。请将此提示发给管理员处理，勿自行关闭不认识的程序。",
        "worker_exited": "后台采集程序已退出，系统正在尝试重新启动。若持续出现，请将此提示发给管理员。",
    }
    state = state if state in messages else "startup_unconfirmed"
    details = messages.get(state, "尚未检测到后台采集程序就绪，具体原因未确认。请保持电脑开机，并将此提示发给管理员；管理员需检查自动启动任务。")
    try:
        free = shutil.disk_usage(data_dir).free
        if free < 1024 * 1024 * 1024:
            details += f"\n\n检测到数据所在磁盘剩余空间仅 {free / (1024 * 1024):.0f} MB，可能影响启动和保存数据。请清理回收站或无关文件，建议留出至少 1 GB；不要删除 HawkHive 采集数据。"
    except OSError:
        pass
    return "自动采集器已安装。\n\n" + details + "\n\n请拍照发送此提示。诊断代码：" + str(state or "startup_unconfirmed")
