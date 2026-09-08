from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from build_release import build_release
from build_test_package import PACKAGE_ROOT, build_test_package
from build_windows_exe_package import (
    PACKAGE_ROOT as WINDOWS_EXE_PACKAGE_ROOT,
)
from build_windows_exe_package import (
    build_windows_exe_package,
)


class ReleasePackageTests(unittest.TestCase):
    def test_cloud_provisioning_requires_an_explicit_device_host(self) -> None:
        root = Path(__file__).resolve().parent
        schema = (root / "cloud_schema.sql").read_text(encoding="utf-8")
        provisioner = (root / "provision_customer.py").read_text(encoding="utf-8")

        self.assertIn("host text NOT NULL,", schema)
        self.assertIn("ALTER TABLE devices ALTER COLUMN host DROP DEFAULT;", schema)
        self.assertIn('parser.add_argument("--device-host", required=True)', provisioner)

    def test_cloud_image_includes_local_runtime_dependencies(self) -> None:
        root = Path(__file__).resolve().parent
        dockerfile = (root / "Dockerfile.cloud").read_text(encoding="utf-8")
        compose = (root / "docker-compose.cloud.yml").read_text(encoding="utf-8")
        ip_caddyfile = (root / "Caddyfile.ip").read_text(encoding="utf-8")
        self.assertIn("postgresql-client-16", dockerfile)
        self.assertIn("COPY release/ ./release/", dockerfile)
        self.assertIn("image: postgres:16", compose)
        self.assertIn("image: caddy:2.11.4-alpine", compose)
        self.assertIn("${DCP_CADDYFILE:-./Caddyfile}", compose)
        self.assertIn("default_sni {$DASHBOARD_DOMAIN}", ip_caddyfile)
        self.assertIn("profile shortlived", ip_caddyfile)
        for dependency in (
            "alarm_config.py",
            "auth_service.py",
            "cloud_api.py",
            "cloud_schema.sql",
            "report_i18n.py",
            "public/i18n.js",
            "public/wallboard.html",
            "public/wallboard.css",
            "public/wallboard.js",
        ):
            self.assertIn(dependency, dockerfile)

    def test_release_contains_source_but_no_runtime_data_or_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "release.zip"
            files, digest = build_release(output)
            second_output = Path(directory) / "release-again.zip"
            _second_files, second_digest = build_release(second_output)
            self.assertIn("dashboard_server.py", files)
            self.assertIn("report_i18n.py", files)
            self.assertIn("public/i18n.js", files)
            self.assertIn("public/collector.js", files)
            self.assertIn("public/wallboard.html", files)
            self.assertIn("public/wallboard.css", files)
            self.assertIn("public/wallboard.js", files)
            self.assertIn("Caddyfile.ip", files)
            self.assertIn("test_i18n_runtime.js", files)
            self.assertNotIn("configure_addvalue.py", files)
            self.assertEqual(len(digest), 64)
            self.assertEqual(second_digest, digest)
            self.assertEqual(second_output.read_bytes(), output.read_bytes())
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
            self.assertTrue(all(name.startswith("dcp8001-source/") for name in names))
            self.assertFalse(any(name.endswith("/.env") for name in names))
            forbidden = ("collector_settings.json", ".sqlite3", ".sqlite3-wal", ".dump", "/backups/")
            self.assertFalse(any(any(marker in name for marker in forbidden) for name in names))
            self.assertTrue(output.with_suffix(".zip.sha256").exists())

    def test_windows_test_package_is_deterministic_complete_and_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "test-package.zip"
            files, digest = build_test_package(output)
            second_output = Path(directory) / "test-package-again.zip"
            _second_files, second_digest = build_test_package(second_output)

            self.assertEqual(digest, second_digest)
            self.assertEqual(output.read_bytes(), second_output.read_bytes())
            for required in (
                "01-INSTALL-TEST-DEPS.cmd",
                "02-RUN-SELF-CHECK.cmd",
                "03-START-DEMO.cmd",
                "04-START-DEVICE-TEST.cmd",
                "TEST-PACKAGE-GUIDE.md",
                "dashboard_server.py",
                "diagnose_rtu_over_tcp.py",
                "public/collector.html",
                "public/wallboard.html",
            ):
                self.assertIn(required, files)

            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                manifest = archive.read(f"{PACKAGE_ROOT}/MANIFEST.sha256").decode("utf-8")
            self.assertTrue(all(name.startswith(f"{PACKAGE_ROOT}/") for name in names))
            self.assertIn("dashboard_server.py", manifest)
            forbidden = (
                "/.env",
                "collector_settings.json",
                ".sqlite3",
                ".sqlite3-wal",
                ".dump",
                "/backups/",
                "/.test-venv/",
            )
            self.assertFalse(any(any(marker in name for marker in forbidden) for name in names))
            self.assertTrue(output.with_suffix(".zip.sha256").exists())

    def test_windows_exe_package_is_deterministic_and_contains_only_delivery_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            fake_exe = temporary / "collector.exe"
            fake_exe.write_bytes(b"MZ-test-executable")
            output = temporary / "windows-exe.zip"
            files, digest = build_windows_exe_package(output, fake_exe)
            second_output = temporary / "windows-exe-again.zip"
            second_files, second_digest = build_windows_exe_package(second_output, fake_exe)

            self.assertEqual(files, second_files)
            self.assertEqual(digest, second_digest)
            self.assertEqual(output.read_bytes(), second_output.read_bytes())
            self.assertEqual(
                set(files),
                {
                    "HawkHive-DPC8001-Collector.exe",
                    "HawkHive-DPC8001-Collector.exe.sha256",
                    "01-START-DEMO.cmd",
                    "02-START-DEVICE.cmd",
                    "WINDOWS-EXE-TEST.md",
                    "WINDOWS-EXE-VERIFICATION.md",
                    "MANIFEST.sha256",
                },
            )
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                self.assertTrue(all(name.startswith(f"{WINDOWS_EXE_PACKAGE_ROOT}/") for name in names))
                self.assertEqual(
                    archive.read(f"{WINDOWS_EXE_PACKAGE_ROOT}/HawkHive-DPC8001-Collector.exe"),
                    fake_exe.read_bytes(),
                )
                manifest = archive.read(
                    f"{WINDOWS_EXE_PACKAGE_ROOT}/MANIFEST.sha256"
                ).decode("ascii")
            self.assertIn("HawkHive-DPC8001-Collector.exe", manifest)
            self.assertNotIn("python-installer", "\n".join(names).casefold())
            self.assertNotIn("windows-build-wheels", "\n".join(names).casefold())
            self.assertTrue(output.with_suffix(".zip.sha256").exists())


if __name__ == "__main__":
    unittest.main()
