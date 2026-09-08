from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from xml.sax.saxutils import escape

from collector_activation import ACTIVATION_FILENAME
from collector_enrollment import ENROLLMENT_FILENAME
from collector_settings import CollectorSettingsError, load_settings

TASK_NAME = "HawkHiveDPC8001Collector"


def install_paths(environ: dict[str, str] | None = None) -> tuple[Path, Path]:
    values = os.environ if environ is None else environ
    program_files = Path(values.get("ProgramFiles", r"C:\Program Files"))
    program_data = Path(values.get("ProgramData", r"C:\ProgramData"))
    return (
        program_files / "HawkHive" / "DPC8001Collector",
        program_data / "HawkHive" / "DPC8001",
    )


def is_administrator() -> bool:
    return bool(os.name == "nt" and ctypes.windll.shell32.IsUserAnAdmin())


def quote_windows_argument(value: str) -> str:
    return subprocess.list2cmdline([value])


def request_elevated_install(
    activation_path: Path | None, enrollment_path: Path | None = None
) -> bool:
    arguments = ["--install-elevated"]
    if activation_path is not None:
        arguments.extend(["--activation", str(activation_path.resolve())])
    if enrollment_path is not None:
        arguments.extend(["--enrollment", str(enrollment_path.resolve())])
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, subprocess.list2cmdline(arguments), None, 1
    )
    return int(result) > 32


def scheduled_task_command(executable: Path, data_dir: Path) -> str:
    return subprocess.list2cmdline(
        [
            str(executable), "--device", "--no-browser", "--data-dir", str(data_dir),
            "--activation", str(data_dir / ACTIVATION_FILENAME),
            "--enrollment", str(data_dir / ENROLLMENT_FILENAME),
        ]
    )


def scheduled_task_arguments(data_dir: Path) -> str:
    return subprocess.list2cmdline(
        [
            "--device", "--no-browser", "--data-dir", str(data_dir),
            "--activation", str(data_dir / ACTIVATION_FILENAME),
            "--enrollment", str(data_dir / ENROLLMENT_FILENAME),
        ]
    )


def scheduled_task_xml(executable: Path, data_dir: Path) -> str:
    """Build a machine-start task with crash recovery and duplicate suppression."""
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>HawkHive DPC8001-G automatic data collector</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="System">
      <UserId>S-1-5-18</UserId>
      <LogonType>ServiceAccount</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>255</Count></RestartOnFailure>
  </Settings>
  <Actions Context="System">
    <Exec>
      <Command>{escape(str(executable))}</Command>
      <Arguments>{escape(scheduled_task_arguments(data_dir))}</Arguments>
    </Exec>
  </Actions>
</Task>
'''


def run_checked(command: list[str], *, allow_failure: bool = False) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode and not allow_failure:
        detail = (completed.stderr or completed.stdout or "command failed").strip()
        raise RuntimeError(detail)


def copy_executable_with_retry(source: Path, destination: Path, timeout: float = 15.0) -> None:
    """Replace an installed EXE after Task Scheduler has released its file lock."""
    staged = destination.with_suffix(destination.suffix + ".new")
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while True:
        try:
            shutil.copy2(source, staged)
            os.replace(staged, destination)
            return
        except OSError as exc:
            last_error = exc
            try:
                staged.unlink()
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "The previous collector did not release its executable; "
                    "stop the HawkHive scheduled task and retry"
                ) from last_error
            time.sleep(0.25)


def install_elevated(
    source_exe: Path,
    activation_path: Path | None,
    enrollment_path: Path | None = None,
) -> Path:
    if os.name != "nt" or not is_administrator():
        raise PermissionError("Administrator permission is required")
    install_dir, data_dir = install_paths()
    install_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    installed_exe = install_dir / "HawkHive-DPC8001-Collector.exe"
    run_checked(["schtasks.exe", "/End", "/TN", TASK_NAME], allow_failure=True)
    if source_exe.resolve() != installed_exe.resolve():
        copy_executable_with_retry(source_exe, installed_exe)
    if activation_path is not None and activation_path.is_file():
        installed_activation = data_dir / ACTIVATION_FILENAME
        if activation_path.resolve() != installed_activation.resolve():
            shutil.copy2(activation_path, installed_activation)
    existing_settings = data_dir / "collector_settings.json"
    try:
        already_configured = load_settings(existing_settings).configured
    except CollectorSettingsError:
        already_configured = False
    if enrollment_path is not None and enrollment_path.is_file() and not already_configured:
        installed_enrollment = data_dir / ENROLLMENT_FILENAME
        if enrollment_path.resolve() != installed_enrollment.resolve():
            shutil.copy2(enrollment_path, installed_enrollment)
    run_checked([
        "icacls.exe", str(data_dir), "/inheritance:r",
        "/grant:r", "SYSTEM:(OI)(CI)F", "Administrators:(OI)(CI)F",
    ])
    handle, task_xml_path = tempfile.mkstemp(
        prefix="hawkhive-task-", suffix=".xml", dir=data_dir
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-16", newline="") as stream:
            stream.write(scheduled_task_xml(installed_exe, data_dir))
        run_checked([
            "schtasks.exe", "/Create", "/TN", TASK_NAME,
            "/XML", task_xml_path, "/F",
        ])
    finally:
        try:
            os.unlink(task_xml_path)
        except OSError:
            pass
    run_checked(["schtasks.exe", "/Run", "/TN", TASK_NAME])
    if (
        activation_path is not None
        and activation_path.is_file()
        and activation_path.resolve() != (data_dir / ACTIVATION_FILENAME).resolve()
    ):
        try:
            activation_path.unlink()
        except OSError:
            # The cloud claim is one-use. Failure to remove the source copy must
            # not roll back a working installation.
            pass
    return installed_exe


def open_console_when_available(
    port: int = 8787, timeout: float = 20.0, *, open_browser: bool = True
) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                if response.status == 200:
                    if open_browser:
                        webbrowser.open(f"http://127.0.0.1:{port}/collector.html", new=1)
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    return False
