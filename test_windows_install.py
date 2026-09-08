from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import windows_install


class WindowsInstallTests(unittest.TestCase):
    def test_install_paths_use_machine_wide_directories(self) -> None:
        install_dir, data_dir = windows_install.install_paths(
            {
                "ProgramFiles": r"D:\Programs",
                "ProgramData": r"D:\SharedData",
            }
        )
        self.assertEqual(install_dir, Path(r"D:\Programs") / "HawkHive" / "DPC8001Collector")
        self.assertEqual(data_dir, Path(r"D:\SharedData") / "HawkHive" / "DPC8001")

    def test_scheduled_task_runs_real_collector_without_browser(self) -> None:
        executable = Path(r"C:\Program Files\HawkHive\DPC8001Collector\collector.exe")
        data_dir = Path(r"C:\ProgramData\HawkHive\DPC8001")
        command = windows_install.scheduled_task_command(executable, data_dir)
        self.assertIn(str(executable), command)
        self.assertIn("--device", command)
        self.assertIn("--no-browser", command)
        self.assertIn("--data-dir", command)
        self.assertIn(str(data_dir), command)
        self.assertIn(str(data_dir / "hawkhive-activation.json"), command)
        self.assertIn(str(data_dir / "hawkhive-enrollment.json"), command)

    def test_task_xml_restarts_failures_and_suppresses_duplicates(self) -> None:
        executable = Path(r"C:\Program Files\HawkHive\DPC8001Collector\collector.exe")
        data_dir = Path(r"C:\ProgramData\HawkHive\DPC8001")
        xml = windows_install.scheduled_task_xml(executable, data_dir)
        self.assertIn("<BootTrigger>", xml)
        self.assertIn("<UserId>S-1-5-18</UserId>", xml)
        self.assertIn("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", xml)
        self.assertIn("<RestartOnFailure><Interval>PT1M</Interval><Count>255</Count>", xml)
        self.assertIn("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", xml)
        self.assertIn("--device", xml)

    def test_elevation_relaunch_forwards_activation_path(self) -> None:
        activation = Path(r"C:\Downloads\HawkHive Collector\hawkhive-activation.json")
        # The quoting helper is the same Windows command-line encoder used by
        # ShellExecuteW, so a folder containing spaces must stay one argument.
        encoded = windows_install.quote_windows_argument(str(activation))
        self.assertIn(str(activation), encoded)
        self.assertTrue(encoded.startswith('"'))

    def test_elevation_relaunch_accepts_reusable_enrollment_path(self) -> None:
        enrollment = Path(r"C:\Downloads\HawkHive Collector\hawkhive-enrollment.json")
        encoded = windows_install.quote_windows_argument(str(enrollment))
        self.assertIn(str(enrollment), encoded)
        self.assertTrue(encoded.startswith('"'))

    def test_executable_copy_uses_a_staged_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "new.exe"
            destination = root / "installed.exe"
            source.write_bytes(b"MZ-new")
            destination.write_bytes(b"MZ-old")
            windows_install.copy_executable_with_retry(source, destination, timeout=0.1)
            self.assertEqual(destination.read_bytes(), b"MZ-new")
            self.assertFalse(destination.with_suffix(".exe.new").exists())


if __name__ == "__main__":
    unittest.main()
