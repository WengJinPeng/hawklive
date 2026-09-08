from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from collector_enrollment import (
    ENROLLMENT_SECRET_FILENAME,
    ENROLLMENT_STATUS_FILENAME,
    enroll_if_available,
    load_enrollment_bundle,
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


def write_bundle(path: Path) -> None:
    path.write_text(
        json.dumps({
            "version": 1,
            "cloud_url": "https://dashboard.example.com/",
            "customer_id": "customer-001",
            "enrollment_token": "enroll" * 8,
        }),
        encoding="utf-8",
    )


class CollectorEnrollmentTests(unittest.TestCase):
    def test_bundle_is_customer_scoped_and_requires_https(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hawkhive-enrollment.json"
            write_bundle(path)
            bundle = load_enrollment_bundle(path)
            self.assertEqual(bundle.cloud_url, "https://dashboard.example.com")
            self.assertEqual(bundle.customer_id, "customer-001")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["cloud_url"] = "http://dashboard.example.com"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                load_enrollment_bundle(path)

    def test_pending_retry_keeps_machine_identity_and_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "hawkhive-enrollment.json"
            settings = root / "collector_settings.json"
            write_bundle(bundle)
            requests: list[dict[str, object]] = []

            def opener(request, timeout=0):
                requests.append(json.loads(request.data.decode("utf-8")))
                return FakeResponse({
                    "ok": True,
                    "data": {"status": "pending"},
                })

            first = enroll_if_available(settings, str(bundle), opener=opener, environ={})
            second = enroll_if_available(settings, str(bundle), opener=opener, environ={})

            self.assertEqual(first.status, "pending")
            self.assertEqual(second.status, "pending")
            self.assertEqual(requests[0]["machine_id"], requests[1]["machine_id"])
            self.assertEqual(requests[0]["claim_secret"], requests[1]["claim_secret"])
            status = json.loads((root / ENROLLMENT_STATUS_FILENAME).read_text("utf-8"))
            self.assertEqual(status, {"status": "pending"})
            self.assertNotIn(requests[0]["claim_secret"], status.values())

    def test_approval_saves_individual_credential_and_removes_installed_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "hawkhive-enrollment.json"
            settings = root / "collector_settings.json"
            write_bundle(bundle)

            def opener(_request, timeout=0):
                return FakeResponse({
                    "ok": True,
                    "data": {
                        "status": "approved", "site_id": "collector-001",
                        "token": "edge" * 8,
                    },
                })

            result = enroll_if_available(settings, str(bundle), opener=opener, environ={})

            self.assertEqual(result.status, "approved")
            self.assertEqual(load_settings(settings).site_id, "collector-001")
            self.assertFalse(bundle.exists())
            self.assertFalse((root / ENROLLMENT_SECRET_FILENAME).exists())
            self.assertFalse((root / ENROLLMENT_STATUS_FILENAME).exists())

    def test_source_package_remains_reusable_after_one_machine_is_approved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "download" / "hawkhive-enrollment.json"
            source.parent.mkdir()
            write_bundle(source)
            settings = root / "installed" / "collector_settings.json"

            def opener(_request, timeout=0):
                return FakeResponse({
                    "ok": True,
                    "data": {
                        "status": "approved", "site_id": "collector-002",
                        "token": "edge" * 8,
                    },
                })

            enroll_if_available(settings, str(source), opener=opener, environ={})
            self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
