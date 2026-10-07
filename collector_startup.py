"""Small, secret-free startup evidence for unattended Windows installations."""
from __future__ import annotations

import errno
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
    # Registration also writes local identity/settings; local I/O is not a
    # network failure. Preserve wrapped errno evidence without exposing text.
    cause = exc
    for _ in range(10):
        if isinstance(cause, OSError):
            if cause.errno == errno.ENOSPC:
                return "storage_full"
            if cause.errno in (errno.EACCES, errno.EPERM):
                return "permission_failed"
        cause = cause.__cause__
        if cause is None:
            break
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 403:
            try:
                body = exc.read(2048).lower()
                if (b"error code: 1010" in body or b"error code: 1020" in body
                        or exc.headers.get("cf-mitigated") == "challenge"):
                    return "cloud_blocked"
            except (OSError, AttributeError):
                pass
        if exc.code in (401, 403, 410):
            return "registration_rejected"
        return "cloud_unavailable" if exc.code >= 500 or exc.code == 429 else "registration_failed"
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        code = getattr(reason, "verify_code", None)
        if code in (2, 18, 19, 20, 21):
            return "tls_untrusted_certificate"
        if code in (9, 10):
            return "tls_certificate_time"
        if code in (62, 64):
            return "tls_hostname_mismatch"
        return "tls_certificate_invalid"
    if isinstance(reason, ssl.SSLError):
        return "tls_handshake_failed"
    if isinstance(exc, urllib.error.URLError) or isinstance(reason, (TimeoutError, ConnectionError)):
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
        "tls_untrusted_certificate": "本机后台未能验证云端证书信任链。程序已自带可信根证书；仍出现时，请将此提示发给管理员检查网络代理或安全软件使用的证书，无需反复校准时间。",
        "tls_certificate_time": "连接使用的证书尚未生效或已过期。请确认电脑时间；已同步仍出现时，请联系管理员检查云端或网络代理证书。",
        "tls_hostname_mismatch": "连接使用的证书与云端地址不匹配。请联系管理员检查云端地址、网络代理或 DNS 设置。",
        "tls_certificate_invalid": "连接使用的证书未通过校验。请将此提示发给管理员检查证书链或网络代理证书。",
        "tls_handshake_failed": "与云端的加密握手中断或协议协商失败。请检查网络连接；持续出现时，请联系管理员检查网络或安全软件的拦截记录。",
        "tls_failed": "本机与云端的安全连接失败。请先校准电脑日期和时间；仍未恢复时，将此提示发给管理员检查证书。",
        "registration_rejected": "云端未接受本机的登记凭据。请联系管理员检查登记权限或获取新的安装包，勿反复安装同一份包。",
        "cloud_blocked": "云端的访问保护规则拦截了本机请求。请将此提示发给管理员检查云端访问策略，无需反复安装。",
        "cloud_unavailable": "云端登记服务暂时不可用。系统会自动重试；持续出现时，请将此提示发给管理员。",
        "registration_failed": "本机登记未完成，暂时无法确定原因。请将此提示发给管理员，系统会自动重试。",
        "starting": "后台程序仍在启动，暂时未检测到本机服务就绪。请保持电脑开机，稍后查看云端是否上线。",
        "ready": "本机服务曾启动，但当前未通过检查。请将此提示发给管理员核查后台运行状态。",
        "storage_full": "磁盘已满，采集程序无法正常写入。请清理回收站或无关文件，不要删除 HawkHive 数据；留出至少 1 GB 后等待自动重试。",
        "permission_failed": "采集程序无法访问运行所需的文件。请将此提示发给管理员检查目录权限或安全软件的拦截记录。",
        "port_in_use": "本机采集服务需要的端口已被占用。请将此提示发给管理员处理，勿自行关闭不认识的程序。",
        "worker_exited": "后台采集程序已退出，系统正在尝试重新启动。若持续出现，请将此提示发给管理员。",
    }
    state = state if isinstance(state, str) and state in messages else "startup_unconfirmed"
    details = messages.get(state, "尚未检测到后台采集程序就绪，具体原因未确认。请保持电脑开机，并将此提示发给管理员；管理员需检查自动启动任务。")
    try:
        usage = shutil.disk_usage(data_dir)
        free = usage.free
        ratio = free / usage.total if usage.total else 0.0
        # Match StorageMaintenance.status(), including percentage thresholds.
        critical = free < 1024**3 or (ratio < 0.03 and free < 5 * 1024**3)
        warning = free < 10 * 1024**3 or (ratio < 0.10 and free < 20 * 1024**3)
        if critical or warning:
            level = "已达到采集器的磁盘保护阈值，可能使启动健康检查不通过" if critical else "磁盘空间偏低，此警告本身不能证明启动失败原因"
            details += f"\n\n数据所在磁盘剩余 {free / (1024**3):.2f} GB（{ratio*100:.1f}%）：{level}。请清理回收站或无关文件，建议留出至少 10 GB 且超过磁盘容量的 10%；不要删除 HawkHive 采集数据。"
    except OSError:
        pass
    return "自动采集器已安装。\n\n" + details + "\n\n请拍照发送此提示。诊断代码：" + str(state or "startup_unconfirmed")
