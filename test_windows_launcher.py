from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import windows_launcher


class WindowsLauncherTests(unittest.TestCase):
    def test_default_data_dir_uses_local_app_data(self) -> None:
        result = windows_launcher.default_data_dir({"LOCALAPPDATA": r"C:\Users\qa\AppData\Local"})
        self.assertEqual(
            result,
            Path(r"C:\Users\qa\AppData\Local") / "HawkHive" / "DPC8001-Collector",
        )

    def test_demo_environment_is_configured_before_server_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            values: dict[str, str] = {}
            selected = windows_launcher.configure_environment(
                demo=True, data_dir=temporary, environ=values
            )
        self.assertEqual(selected, Path(temporary).resolve())
        self.assertEqual(values["DCP_DATA_DIR"], str(Path(temporary).resolve()))
        self.assertEqual(values["DCP_DEMO_MODE"], "1")
        self.assertEqual(values["DCP_POLL_SECONDS"], "2")
        self.assertEqual(values["DCP_RECORD_SECONDS"], "10")

    def test_device_mode_does_not_set_demo_timing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            values: dict[str, str] = {}
            windows_launcher.configure_environment(
                demo=False, data_dir=temporary, environ=values
            )
        self.assertEqual(values["DCP_DEMO_MODE"], "0")
        self.assertNotIn("DCP_POLL_SECONDS", values)
        self.assertNotIn("DCP_RECORD_SECONDS", values)

    def test_cli_defaults_to_real_device_mode(self) -> None:
        args = windows_launcher.parse_args([])
        self.assertFalse(args.device)
        self.assertFalse(args.demo)
        self.assertFalse(args.no_browser)
        self.assertEqual(args.port, 8787)
        self.assertIsNone(args.enrollment)

    def test_device_mode_enables_automatic_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            values: dict[str, str] = {}
            windows_launcher.configure_environment(
                demo=False, data_dir=temporary, environ=values
            )
        self.assertEqual(values["DCP_REMOTE_DISCOVERY_ENABLED"], "1")


if __name__ == "__main__":
    unittest.main()
