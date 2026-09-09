from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from collector_activation import activate_if_available, activation_candidates
from collector_enrollment import (
    enroll_if_available,
    enrollment_candidates,
    enrollment_status,
)
from collector_settings import CollectorSettingsError, load_settings
from single_instance import AlreadyRunningError
from windows_install import (
    install_paths,
    install_elevated,
    open_console_when_available,
    request_elevated_install,
)

DEFAULT_PORT = 8787


def default_data_dir(environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    local_app_data = values.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "HawkHive" / "DPC8001-Collector"
    return Path.home() / "HawkHive" / "DPC8001-Collector"


def configure_environment(
    *, demo: bool, data_dir: str | None = None, environ: dict[str, str] | None = None
) -> Path:
    values = os.environ if environ is None else environ
    selected = Path(data_dir).expanduser() if data_dir else default_data_dir(values)
    selected = selected.resolve()
    values["DCP_DATA_DIR"] = str(selected)
    values["DCP_DEMO_MODE"] = "1" if demo else "0"
    if demo:
        values.setdefault("DCP_POLL_SECONDS", "2")
        values.setdefault("DCP_RECORD_SECONDS", "10")
    else:
        values.setdefault("DCP_REMOTE_DISCOVERY_ENABLED", "1")
    return selected


def open_console_when_ready(port: int, timeout: float = 20.0) -> None:
    url = f"http://127.0.0.1:{port}/collector.html"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url, new=1)
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the HawkHive DPC8001-G collector.")
    from cloud_sync import COLLECTOR_VERSION
    parser.add_argument("--version", action="version", version=COLLECTOR_VERSION)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="Use generated demo readings")
    mode.add_argument("--device", action="store_true", help="Read configured DPC8001-G devices")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", help="Override the local runtime data directory")
    parser.add_argument("--activation", help="Use a downloaded one-time activation file")
    parser.add_argument("--enrollment", help="Use a reusable customer enrollment file")
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--managed-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--install-elevated", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-browser", action="store_true", help="Do not open the local console")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("Port must be between 1 and 65535")
    if args.supervise:
        from collector_updater import run_supervisor
        return run_supervisor(Path(args.data_dir).resolve(), args.port)
    activation_path = next(
        (path for path in activation_candidates(args.activation) if path.is_file()),
        None,
    )
    enrollment_path = next(
        (path for path in enrollment_candidates(args.enrollment) if path.is_file()),
        None,
    )
    if os.name == "nt" and args.install_elevated:
        try:
            installed = install_elevated(
                Path(sys.executable).resolve(), activation_path, enrollment_path
            )
            print(f"Installed automatic collector: {installed}")
            import ctypes
            _install_dir, installed_data_dir = install_paths()
            status = None
            deadline = time.monotonic() + 30
            try:
                already_configured = load_settings(
                    installed_data_dir / "collector_settings.json"
                ).configured
            except CollectorSettingsError:
                already_configured = False
            while (
                enrollment_path is not None
                and not already_configured
                and time.monotonic() < deadline
            ):
                status = enrollment_status(installed_data_dir / "collector_settings.json")
                if status:
                    break
                try:
                    if load_settings(
                        installed_data_dir / "collector_settings.json"
                    ).configured:
                        break
                except CollectorSettingsError:
                    pass
                time.sleep(0.5)
            if status and status.get("status") == "pending":
                message = (
                    "自动采集器安装成功。\n\n"
                    "本机已自动提交到云端，正在等待管理员决定是否加入。\n\n"
                    "管理员批准并填写采集器名称后，本机会自动开始工作。"
                )
                ctypes.windll.user32.MessageBoxW(None, message, "HawkHive 等待确认", 0x40)
                return 0
            if open_console_when_available(timeout=30, open_browser=False):
                message = "自动采集器安装并启动成功。现在可以回到云端等待设备出现。"
                ctypes.windll.user32.MessageBoxW(None, message, "HawkHive 安装成功", 0x40)
                return 0
            message = (
                "自动采集器已经安装，但首次启动尚未成功。系统会每分钟自动重试。\n\n"
                "请确认这台电脑可以访问云端；若持续未上线，请联系管理员查看任务计划程序中的 "
                "HawkHiveDPC8001Collector。"
            )
            ctypes.windll.user32.MessageBoxW(None, message, "HawkHive 等待连接", 0x30)
            return 5
        except Exception as exc:
            message = f"自动安装失败。\n\n详细信息：{exc}"
            print("INSTALL_FAILED: " + message)
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "HawkHive 自动安装失败", 0x10)
            return 4
    if (
        os.name == "nt"
        and (activation_path is not None or enrollment_path is not None)
        and not args.demo
        and not args.managed_worker
    ):
        if request_elevated_install(activation_path, enrollment_path):
            return 0
        message = "需要管理员权限才能安装开机自动采集。请在权限提示中选择“是”。"
        print("INSTALL_PERMISSION_REQUIRED: " + message)
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "HawkHive 需要管理员权限", 0x30)
        return 4
    data_dir = configure_environment(demo=args.demo, data_dir=args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    if args.managed_worker:
        from update_protocol import read_json
        def watch_supervisor():
            while True:
                command = read_json(data_dir / "collector-supervisor-stop.json")
                if os.environ.get("DCP_SUPERVISOR_NONCE") and command.get("nonce") == os.environ["DCP_SUPERVISOR_NONCE"]:
                    import dashboard_server
                    if dashboard_server.HTTP_SERVER is not None:
                        dashboard_server.request_shutdown()
                        return
                time.sleep(0.5)
        threading.Thread(target=watch_supervisor, daemon=True).start()
    enrolled = None
    while True:
        try:
            activated = activate_if_available(
                data_dir / "collector_settings.json", args.activation
            )
            enrolled = enroll_if_available(
                data_dir / "collector_settings.json", args.enrollment
            )
            if enrolled is not None and enrolled.status == "pending":
                print("CLOUD_APPROVAL_REQUIRED: collector registration submitted")
                if os.name == "nt" and args.no_browser:
                    time.sleep(10)
                    continue
                return 0
            break
        except Exception as exc:
            message = (
                "自动登记失败。请确认电脑可以访问云端；系统会自动重试。\n\n"
                f"详细信息：{exc}"
            )
            print("ENROLLMENT_FAILED: " + message)
            if os.name == "nt" and args.no_browser and (
                activation_path is not None or enrollment_path is not None
            ):
                # The SYSTEM task must survive a long first-day Internet outage.
                # A fresh package installation will stop this task and replace
                # the expired activation file when recovery needs administrator action.
                time.sleep(60)
                continue
            if os.name == "nt":
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None, message, "HawkHive 自动激活失败", 0x10
                )
            return 3

    # Monitor defaults are resolved while importing dashboard_server, so the
    # environment must be configured before this import.
    import dashboard_server

    print("HawkHive DPC8001-G Collector")
    print(f"Mode: {'demo' if args.demo else 'device'}")
    if activated:
        print(f"Activated cloud site: {activated.site_id}")
    if enrolled is not None and enrolled.settings is not None:
        print(f"Enrolled cloud site: {enrolled.settings.site_id}")
    print(f"Data: {data_dir}")
    print("Keep this window open while the portable collector is running.")
    if not args.no_browser:
        threading.Thread(
            target=open_console_when_ready, args=(args.port,), daemon=True
        ).start()
    try:
        return dashboard_server.run_collector("127.0.0.1", args.port)
    except AlreadyRunningError:
        message = "采集器已在运行，不能重复启动。\n请使用原采集器窗口或联系管理员查看后台服务。\n原有采集和上传不会受到影响。"
        print("ALREADY_RUNNING: " + message)
        if os.name == "nt" and not args.no_browser:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "HawkHive 采集器已启动", 0x40)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
