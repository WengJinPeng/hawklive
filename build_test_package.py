from __future__ import annotations

import argparse
import hashlib
import os
import zipfile
from pathlib import Path

from build_release import ROOT, assert_safe_release_path

PACKAGE_ROOT = "DPC8001-G-Windows-Test"
TEST_PACKAGE_FILES = {
    "01-INSTALL-TEST-DEPS.cmd",
    "02-RUN-SELF-CHECK.cmd",
    "03-START-DEMO.cmd",
    "04-START-DEVICE-TEST.cmd",
    "AUTOMATED-ONBOARDING.md",
    "DOCUMENT-CONFLICTS-DPC8001-G.md",
    "OPEN-QUESTIONS-MANUFACTURER-CUSTOMER.md",
    "TEST-PACKAGE-GUIDE.md",
    "TEST-PACKAGE-VERIFICATION.md",
    "WINDOWS-DEPLOYMENT.md",
    "alarm_config.py",
    "auth_service.py",
    "backup_scheduler.py",
    "cloud_backup.py",
    "cloud_sync.py",
    "collector_activation.py",
    "collector_enrollment.py",
    "collector_settings.py",
    "dashboard_server.py",
    "dcp8001_collector.py",
    "device_discovery.py",
    "diagnose_rtu_over_tcp.py",
    "install-test-dependencies.ps1",
    "install-windows-service.ps1",
    "monitoring_service.py",
    "report_i18n.py",
    "requirements-test-windows.txt",
    "requirements-windows.txt",
    "requirements.txt",
    "run-test-self-check.ps1",
    "runtime_paths.py",
    "single_instance.py",
    "start-test-package.ps1",
    "storage_maintenance.py",
    "test_collector_settings.py",
    "test_collector_activation.py",
    "test_collector_enrollment.py",
    "test_collector_enrollment_postgres.py",
    "test_dcp8001_collector.py",
    "test_diagnose_rtu_over_tcp.py",
    "test_i18n.py",
    "test_monitoring.py",
    "test_runtime_foundation.py",
    "test_storage_maintenance.py",
    "test_backup_instance_safety.py",
    "test_wallboard_contract.py",
    "uninstall-windows-service.ps1",
    "windows_launcher.py",
    "windows_install.py",
    "windows_service.py",
    "test_windows_install.py",
    "test_windows_launcher.py",
}
PUBLIC_SUFFIXES = {".html", ".css", ".js"}


def test_package_files() -> list[Path]:
    files = [ROOT / name for name in sorted(TEST_PACKAGE_FILES)]
    public = ROOT / "public"
    files.extend(
        path
        for path in public.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix in PUBLIC_SUFFIXES
    )
    missing = [path.name for path in files if not path.is_file()]
    if missing:
        raise RuntimeError(f"Test package source files are missing: {', '.join(sorted(missing))}")
    return sorted(files, key=lambda path: path.relative_to(ROOT).as_posix())


def _zip_info(name: str, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.external_attr = (0o755 if executable else 0o644) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def build_test_package(output: Path) -> tuple[list[str], str]:
    output = output.resolve()
    if output.suffix.casefold() != ".zip":
        raise ValueError("Test package output must use the .zip extension")
    files = test_package_files()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".zip.tmp")
    manifest_lines: list[str] = []
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            assert_safe_release_path(path)
            relative = path.relative_to(ROOT).as_posix()
            content = path.read_bytes()
            manifest_lines.append(f"{hashlib.sha256(content).hexdigest()}  {relative}")
            archive.writestr(
                _zip_info(f"{PACKAGE_ROOT}/{relative}", executable=os.access(path, os.X_OK)),
                content,
            )
        manifest = ("\n".join(manifest_lines) + "\n").encode("utf-8")
        archive.writestr(_zip_info(f"{PACKAGE_ROOT}/MANIFEST.sha256"), manifest)
    temporary.replace(output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{digest}  {output.name}\n", encoding="utf-8"
    )
    return [path.relative_to(ROOT).as_posix() for path in files], digest


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the DPC8001-G Windows field-test package.")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "DPC8001-G-Windows-Test.zip",
        help="Output ZIP path",
    )
    args = parser.parse_args()
    files, digest = build_test_package(args.output)
    print(f"Packaged {len(files)} test files plus MANIFEST.sha256")
    print(f"SHA-256: {digest}")
    print(f"Output: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
