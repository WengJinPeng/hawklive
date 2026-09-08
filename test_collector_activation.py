from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from collector_activation import (
    activate_if_available,
    activation_candidates,
    load_activation_bundle,
    load_or_create_installation_id,
)
from collector_settings import load_settings


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body.read()


class CollectorActivationTests(unittest.TestCase):
    def test_bundle_requires_https_and_complete_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activation.json"
            path.write_text(
                json.dumps({
                    "version": 1,
                    "cloud_url": "https://dashboard.example.com/",
                    "activation_token": "a" * 32,
                }),
                encoding="utf-8",
            )
            bundle = load_activation_bundle(path)
            self.assertEqual(bundle.cloud_url, "https://dashboard.example.com")
            self.assertEqual(bundle.activation_token, "a" * 32)

            path.write_text(
                json.dumps({
                    "version": 1,
                    "cloud_url": "http://dashboard.example.com",
                    "activation_token": "a" * 32,
                }),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                load_activation_bundle(path)

    def test_activation_is_claimed_saved_and_secret_file_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle_path = root / "hawkhive-activation.json"
            settings_path = root / "collector_settings.json"
            bundle_path.write_text(
                json.dumps({
                    "version": 1,
                    "cloud_url": "https://dashboard.example.com",
                    "activation_token": "claim" * 8,
                }),
                encoding="utf-8",
            )
            seen: dict[str, object] = {}

            def opener(request, timeout=0):
                seen["url"] = request.full_url
                seen["body"] = json.loads(request.data.decode("utf-8"))
                seen["timeout"] = timeout
                return FakeResponse({
                    "ok": True,
                    "data": {"site_id": "collector-001", "token": "edge" * 8},
                })

            settings = activate_if_available(
                settings_path, str(bundle_path), opener=opener,
                environ={}, executable=str(root / "collector.exe"),
            )

            self.assertIsNotNone(settings)
            self.assertEqual(seen["url"], "https://dashboard.example.com/api/v1/edge/activate")
            self.assertEqual(seen["body"]["activation_token"], "claim" * 8)
            self.assertRegex(seen["body"]["machine_id"], r"^[0-9a-f]{32}$")
            self.assertFalse(bundle_path.exists())
            self.assertEqual(load_settings(settings_path).site_id, "collector-001")

    def test_installation_identity_is_stable_across_activation_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "collector_settings.json"
            first = load_or_create_installation_id(settings_path)
            second = load_or_create_installation_id(settings_path)
            self.assertEqual(first, second)
            self.assertRegex(first, r"^[0-9a-f]{32}$")

    def test_existing_settings_do_not_consume_another_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "collector_settings.json"
            from collector_settings import save_settings, validate_settings

            save_settings(
                settings_path,
                validate_settings("https://example.com", "site-001", "x" * 32),
            )
            bundle = root / "hawkhive-activation.json"
            bundle.write_text("{}", encoding="utf-8")
            result = activate_if_available(settings_path, str(bundle))
            self.assertIsNone(result)
            self.assertTrue(bundle.exists())

    def test_executable_directory_is_checked_before_working_directory(self) -> None:
        candidates = activation_candidates(
            environ={}, executable=r"C:\Program Files\HawkHive\collector.exe"
        )
        self.assertEqual(candidates[0].name, "hawkhive-activation.json")


if __name__ == "__main__":
    unittest.main()
