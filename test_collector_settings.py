from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from collector_settings import (
    CollectorSettingsError,
    load_settings,
    save_settings,
    validate_settings,
)


class CollectorSettingsTests(unittest.TestCase):
    def test_valid_settings_are_saved_and_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            expected = validate_settings(
                "https://dashboard.example.com/", "addvalue-site-001", "x" * 32
            )
            save_settings(path, expected)
            self.assertEqual(load_settings(path), expected)
            payload = json.loads(path.read_text())
            self.assertEqual(payload["cloud_url"], "https://dashboard.example.com")
            self.assertNotIn("token", payload)
            self.assertEqual(payload["token_protection"],
                             "windows-dpapi-machine-v1" if os.name == "nt" else "plaintext-v1")
            if os.name == "nt":
                self.assertNotIn(expected.token, path.read_text())

    def test_public_dict_never_returns_token(self) -> None:
        settings = validate_settings("https://example.com", "site-001", "secret" * 5)
        public = settings.public_dict()
        self.assertTrue(public["token_configured"])
        self.assertNotIn("token", public)

    def test_rejects_insecure_remote_url(self) -> None:
        with self.assertRaises(ValueError):
            validate_settings("http://example.com", "site-001", "x" * 32)

    def test_existing_corrupt_settings_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"cloud_url":"https://example.com"}', encoding="utf-8")
            with self.assertRaises(CollectorSettingsError):
                load_settings(path)


if __name__ == "__main__":
    unittest.main()
