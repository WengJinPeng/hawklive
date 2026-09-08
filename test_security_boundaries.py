from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

import dashboard_server as dashboard_module
from cloud_api import (
    AlarmEventIn,
    CollectorHeartbeat,
    DiscoveredDeviceAssignment,
    DiscoveryResultIn,
    LatestReadingIn,
    ReadingIn,
    admin_assign_discovered_device,
    app,
    authorize_ingest_target,
    build_collector_package,
    build_reusable_collector_package,
    claim_collector_activation,
    collector_public_url,
    customer_enrollment_token,
    configuration_for_site,
    customer_history,
    customer_trends,
    parse_scope_parameter,
    published_collector_executable,
    reissue_collector_activation,
    topology_manager,
)
from dashboard_server import DashboardHandler, can_manage_topology


class QuietDashboardHandler(DashboardHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        pass


class CloudReadPathTests(unittest.TestCase):
    def test_cloud_exposes_history_and_trend_routes(self) -> None:
        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertIn("/api/history", paths)
        self.assertIn("/api/trends", paths)
        self.assertIn("/api/history/export-xlsx", paths)
        self.assertIn("/api/v1/edge/activate", paths)
        self.assertIn("/api/v1/edge/enroll", paths)
        self.assertIn("/api/admin/collector-installer", paths)
        self.assertIn("/api/admin/collector-installer/rotate", paths)
        self.assertIn("/api/admin/pending-collectors", paths)
        self.assertIn("/api/admin/pending-collectors/{enrollment_id}/approve", paths)
        self.assertIn("/api/admin/collector-package", paths)
        self.assertIn("/api/admin/collectors/{site_id}/package", paths)
        self.assertIn("/api/admin/discovered-devices", paths)
        self.assertIn("/api/admin/discovered-devices/assign", paths)

    def test_scope_parameters_are_deduplicated(self) -> None:
        self.assertEqual(parse_scope_parameter("a,b,a"), ["a", "b"])

    @patch("cloud_api.apply_display_names", side_effect=lambda rows, _customer_id: rows)
    @patch("cloud_api.connect")
    def test_history_query_applies_room_and_device_scope(self, connect, _display_names) -> None:
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = []
        connect.return_value.__enter__.return_value = db

        result = customer_history(
            cleanroom_ids="room-a,room-b",
            device_ids="device-a",
            start=1,
            end=2,
            limit=25,
            user={"customer_id": "customer-a"},
        )

        self.assertEqual(result, {"ok": True, "data": []})
        query, params = db.execute.call_args.args
        self.assertIn("cleanroom_id IN (%s,%s)", query)
        self.assertIn("device_id IN (%s)", query)
        self.assertEqual(params, ("customer-a", "room-a", "room-b", "device-a", 1, 2, 25))

    @patch("cloud_api.apply_display_names", side_effect=lambda rows, _customer_id: rows)
    @patch("cloud_api.connect")
    def test_trend_query_is_evenly_sampled_per_device(self, connect, _display_names) -> None:
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = []
        connect.return_value.__enter__.return_value = db

        result = customer_trends(
            cleanroom_ids="room-a",
            device_ids="device-a,device-b",
            start=1,
            end=2,
            max_points=40,
            user={"customer_id": "customer-a"},
        )

        self.assertEqual(result, {"ok": True, "data": []})
        query, params = db.execute.call_args.args
        self.assertIn("PARTITION BY sample_device_key", query)
        self.assertIn("cleanroom_id IN (%s)", query)
        self.assertIn("device_id IN (%s,%s)", query)
        self.assertEqual(params, ("customer-a", 1, 2, "room-a", "device-a", "device-b", 40))


class CollectorRequestSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietDashboardHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        method: str,
        path: str,
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        request_headers = {"Host": f"127.0.0.1:{self.server.server_port}", **(headers or {})}
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload
        finally:
            connection.close()

    def csrf_token(self) -> str:
        status, payload = self.request("GET", "/api/collector/session")
        self.assertEqual(status, 200)
        return str(payload["data"]["csrf_token"])

    def test_local_session_issues_a_nonempty_request_token(self) -> None:
        self.assertGreater(len(self.csrf_token()), 20)

    def test_mutation_without_request_token_is_rejected(self) -> None:
        status, _payload = self.request(
            "POST", "/api/collector/cloud", "{}", {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)

    def test_cross_origin_mutation_is_rejected_even_with_token(self) -> None:
        status, _payload = self.request(
            "POST",
            "/api/collector/cloud",
            "{}",
            {
                "Content-Type": "application/json",
                "Origin": "https://attacker.example",
                "X-DCP-CSRF": self.csrf_token(),
            },
        )
        self.assertEqual(status, 403)

    def test_non_json_mutation_is_rejected_even_with_token(self) -> None:
        status, _payload = self.request(
            "POST",
            "/api/collector/cloud",
            "{}",
            {"Content-Type": "text/plain", "X-DCP-CSRF": self.csrf_token()},
        )
        self.assertEqual(status, 415)

    def test_non_local_host_header_is_rejected(self) -> None:
        status, _payload = self.request(
            "GET", "/api/collector/session", headers={"Host": "collector.attacker.example"},
        )
        self.assertEqual(status, 403)

    def test_viewer_cannot_call_local_topology_or_configuration_mutations(self) -> None:
        class ViewerAuth:
            @staticmethod
            def user_for_token(_token: str | None) -> dict[str, object]:
                return {
                    "id": "viewer-001", "customer_id": "customer-001",
                    "username": "viewer", "display_name": "Viewer", "role": "viewer",
                }

        previous_auth = dashboard_module.AUTH
        dashboard_module.AUTH = ViewerAuth()  # type: ignore[assignment]
        headers = {"Content-Type": "application/json", "Cookie": "dcp_session=test"}
        try:
            for path in ("/api/admin/cleanrooms", "/api/admin/devices", "/api/config"):
                status, payload = self.request("POST", path, "{}", headers)
                self.assertEqual(status, 403, path)
                self.assertFalse(payload["ok"], path)
        finally:
            dashboard_module.AUTH = previous_auth

    def test_local_admin_gets_an_empty_cloud_discovery_inbox(self) -> None:
        class AdminAuth:
            @staticmethod
            def user_for_token(_token: str | None) -> dict[str, object]:
                return {
                    "id": "admin-001", "customer_id": "customer-001",
                    "username": "admin", "display_name": "Admin",
                    "role": "customer_admin",
                }

        previous_auth = dashboard_module.AUTH
        dashboard_module.AUTH = AdminAuth()  # type: ignore[assignment]
        try:
            status, payload = self.request(
                "GET", "/api/admin/discovered-devices",
                headers={"Cookie": "dcp_session=test"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload, {"ok": True, "data": []})
        finally:
            dashboard_module.AUTH = previous_auth

    def test_local_admin_gets_an_empty_cloud_collector_join_inbox(self) -> None:
        class AdminAuth:
            @staticmethod
            def user_for_token(_token: str | None) -> dict[str, object]:
                return {
                    "id": "admin-001", "customer_id": "customer-001",
                    "username": "admin", "display_name": "Admin",
                    "role": "customer_admin",
                }

        previous_auth = dashboard_module.AUTH
        dashboard_module.AUTH = AdminAuth()  # type: ignore[assignment]
        try:
            status, payload = self.request(
                "GET", "/api/admin/pending-collectors",
                headers={"Cookie": "dcp_session=test"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload, {"ok": True, "data": []})
        finally:
            dashboard_module.AUTH = previous_auth


class CloudSourceValidationTests(unittest.TestCase):
    def reading_payload(self) -> dict[str, object]:
        return {
            "record_uuid": "11111111-1111-4111-8111-111111111111",
            "customer_id": "customer-001",
            "site_id": "site-001",
            "cleanroom_id": "room-001",
            "cleanroom_name": "Cleanroom 1",
            "device_id": "device-001",
            "device_name": "Device 1",
            "measured_at": 1000.0,
            "source": "device",
            "particles": {"pm_0_5_um": 1},
            "environment": {"temperature": 22.0, "humidity": 50.0},
            "alarm_status": "NORMAL",
            "alarm_details": [],
        }

    def test_cloud_reading_model_rejects_demo_source(self) -> None:
        payload = self.reading_payload()
        ReadingIn(**payload)
        payload["source"] = "demo"
        with self.assertRaises(ValidationError):
            ReadingIn(**payload)

    def test_cloud_models_reject_mismatched_or_unsupported_particle_units(self) -> None:
        payload = self.reading_payload()
        payload.update({"particle_unit_code": 1, "particle_unit_label": "PCS/m3"})
        with self.assertRaises(ValidationError):
            ReadingIn(**payload)

        latest_payload = dict(payload)
        latest_payload.pop("record_uuid")
        latest_payload.update({"particle_unit_code": 2, "particle_unit_label": "PCS/m3"})
        with self.assertRaises(ValidationError):
            LatestReadingIn(**latest_payload)

    def test_cloud_alarm_model_rejects_demo_source(self) -> None:
        payload = {
            "event_uuid": "22222222-2222-4222-8222-222222222222",
            "customer_id": "customer-001",
            "site_id": "site-001",
            "cleanroom_id": "room-001",
            "device_id": "device-001",
            "source": "demo",
            "metric": "temperature",
            "started_at": 1000.0,
            "limit_description": "outside range",
        }
        with self.assertRaises(ValidationError):
            AlarmEventIn(**payload)

    def test_discovery_verification_requires_protocol_and_supported_unit_evidence(self) -> None:
        valid = {
            "host": "192.168.1.88", "tcp_port": 502, "slave": 1,
            "verified": True, "modbus_responded": True,
            "protocol_compatible": True, "firmware_raw": 100,
            "particle_unit_code": 1, "particle_unit_label": "PCS/28.3L",
            "unit_supported": True, "latency_ms": 12,
        }
        DiscoveryResultIn(**valid)

        invalid = dict(valid)
        invalid.update({
            "particle_unit_code": 2, "particle_unit_label": "PCS/m3",
            "unit_supported": False,
        })
        with self.assertRaises(ValidationError):
            DiscoveryResultIn(**invalid)


class CloudIngestAuthorizationTests(unittest.TestCase):
    def test_ingest_target_requires_enabled_device_in_token_site_and_uses_canonical_names(self) -> None:
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = ("Canonical room", "Canonical device")
        identity = {"customer_id": "customer-001", "site_id": "site-001"}

        names = authorize_ingest_target(db, identity, "room-001", "device-001")

        self.assertEqual(names, ("Canonical room", "Canonical device"))
        params = db.execute.call_args.args[1]
        self.assertEqual(
            params,
            ("device-001", "customer-001", "site-001", "room-001", "customer-001"),
        )

    def test_unknown_or_cross_site_device_is_rejected(self) -> None:
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None
        with self.assertRaises(HTTPException) as raised:
            authorize_ingest_target(
                db,
                {"customer_id": "customer-001", "site_id": "site-001"},
                "room-001",
                "device-from-another-site",
            )
        self.assertEqual(raised.exception.status_code, 403)


class AutomatedOnboardingSecurityTests(unittest.TestCase):
    ACTIVATION_SECRET = "ab" * 32

    def test_package_uses_canonical_public_url_instead_of_request_host(self) -> None:
        request = MagicMock()
        request.base_url = "https://attacker.invalid/"
        with patch.dict(os.environ, {"DCP_PUBLIC_URL": "https://dashboard.example.com/"}):
            self.assertEqual(
                collector_public_url(request), "https://dashboard.example.com"
            )

    def test_remote_package_url_fails_closed_without_https_configuration(self) -> None:
        request = MagicMock()
        request.base_url = "http://dashboard.example.com/"
        with patch.dict(
            os.environ,
            {"DCP_PUBLIC_URL": "", "DASHBOARD_SECURE_COOKIE": "1"},
        ), self.assertRaises(HTTPException) as raised:
            collector_public_url(request)
        self.assertEqual(raised.exception.status_code, 503)

    @patch("cloud_api.connect")
    def test_package_retry_revokes_only_unconsumed_claims(self, connect) -> None:
        db = MagicMock()
        db.execute.return_value.fetchone.side_effect = [("Workshop collector",), None]
        connect.return_value.__enter__.return_value = db

        token = reissue_collector_activation(
            "customer-001", "collector-001", actor_user_id="11111111-1111-4111-8111-111111111111"
        )

        self.assertGreaterEqual(len(token), 24)
        statements = [call.args[0] for call in db.execute.call_args_list]
        self.assertTrue(any("DELETE FROM collector_activation_tokens" in sql for sql in statements))
        self.assertTrue(any("INSERT INTO collector_activation_tokens" in sql for sql in statements))

    @patch("cloud_api.connect")
    def test_activated_collector_cannot_issue_a_second_package(self, connect) -> None:
        db = MagicMock()
        db.execute.return_value.fetchone.side_effect = [("Workshop collector",), (1,)]
        connect.return_value.__enter__.return_value = db

        with self.assertRaises(HTTPException) as raised:
            reissue_collector_activation(
                "customer-001", "collector-001", actor_user_id="11111111-1111-4111-8111-111111111111"
            )

        self.assertEqual(raised.exception.status_code, 409)
        statements = [call.args[0] for call in db.execute.call_args_list]
        self.assertFalse(any("DELETE FROM collector_activation_tokens" in sql for sql in statements))

    @patch("cloud_api.connect")
    def test_activation_claim_is_bound_before_durable_token_is_created(self, connect) -> None:
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = (
            "customer-001", "collector-001", None, None,
        )
        connect.return_value.__enter__.return_value = db

        with patch.dict(os.environ, {"DCP_ACTIVATION_SECRET": self.ACTIVATION_SECRET}):
            result = claim_collector_activation(
                "claim-token" * 3, "1" * 32, "WIN-EDGE", "Windows 11"
            )

        self.assertEqual(result["site_id"], "collector-001")
        self.assertGreaterEqual(len(result["token"]), 24)
        statements = [call.args[0] for call in db.execute.call_args_list]
        self.assertTrue(any("UPDATE collector_activation_tokens" in sql for sql in statements))
        self.assertTrue(any("INSERT INTO edge_tokens" in sql for sql in statements))
        self.assertFalse(any("claim-token" in str(call.args) for call in db.execute.call_args_list))

    @patch("cloud_api.connect")
    def test_invalid_or_reused_activation_fails_closed(self, connect) -> None:
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None
        connect.return_value.__enter__.return_value = db
        with patch.dict(os.environ, {"DCP_ACTIVATION_SECRET": self.ACTIVATION_SECRET}):
            with self.assertRaises(HTTPException) as raised:
                claim_collector_activation(
                    "expired-token" * 3, "1" * 32, "WIN-EDGE", "Windows 11"
                )
        self.assertEqual(raised.exception.status_code, 401)

    @patch("cloud_api.connect")
    def test_same_installation_can_retry_a_claim_but_another_machine_cannot(self, connect) -> None:
        db = MagicMock()
        db.execute.return_value.fetchone.side_effect = [
            ("customer-001", "collector-001", object(), "1" * 32),
            (1,),
            ("customer-001", "collector-001", object(), "1" * 32),
            (1,),
            ("customer-001", "collector-001", object(), "1" * 32),
        ]
        connect.return_value.__enter__.return_value = db
        with patch.dict(os.environ, {"DCP_ACTIVATION_SECRET": self.ACTIVATION_SECRET}):
            first = claim_collector_activation(
                "claim-token" * 3, "1" * 32, "WIN-EDGE", "Windows 11"
            )
            second = claim_collector_activation(
                "claim-token" * 3, "1" * 32, "WIN-EDGE", "Windows 11"
            )
            self.assertEqual(first, second)
            with self.assertRaises(HTTPException) as raised:
                claim_collector_activation(
                    "claim-token" * 3, "2" * 32, "OTHER-EDGE", "Windows 11"
                )
        self.assertEqual(raised.exception.status_code, 401)

    def test_activation_secret_is_required(self) -> None:
        with patch.dict(os.environ, {"DCP_ACTIVATION_SECRET": ""}, clear=False):
            with self.assertRaises(HTTPException) as raised:
                claim_collector_activation(
                    "claim-token" * 3, "1" * 32, "WIN-EDGE", "Windows 11"
                )
        self.assertEqual(raised.exception.status_code, 503)

    def test_reusable_enrollment_token_is_customer_and_generation_scoped(self) -> None:
        with patch.dict(os.environ, {"DCP_ACTIVATION_SECRET": self.ACTIVATION_SECRET}):
            first = customer_enrollment_token("customer-001", 1)
            self.assertEqual(first, customer_enrollment_token("customer-001", 1))
            self.assertNotEqual(first, customer_enrollment_token("customer-002", 1))
            self.assertNotEqual(first, customer_enrollment_token("customer-001", 2))

    @patch("cloud_api.create_cloud_device")
    @patch("cloud_api.connect")
    @patch("cloud_api.customer_configuration", return_value=[])
    def test_assignment_requires_server_side_verified_discovery(
        self, _configuration, connect, create_device
    ) -> None:
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None
        connect.return_value.__enter__.return_value = db
        payload = DiscoveredDeviceAssignment(
            site_id="collector-001", cleanroom_id="room-001", name="Counter A",
            host="192.168.2.30", tcp_port=502, slave=1,
        )
        with self.assertRaises(HTTPException) as raised:
            admin_assign_discovered_device(
                payload,
                {"id": "11111111-1111-4111-8111-111111111111", "customer_id": "customer-001"},
            )
        self.assertEqual(raised.exception.status_code, 409)
        create_device.assert_not_called()

    @patch("cloud_api.connect")
    def test_customer_package_contains_exe_and_short_lived_claim_only(self, connect) -> None:
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = (1,)
        connect.return_value.__enter__.return_value = db
        with tempfile.TemporaryDirectory() as directory:
            exe = os.path.join(directory, "collector.exe")
            with open(exe, "wb") as stream:
                stream.write(b"MZ-test-executable")
            digest = hashlib.sha256(b"MZ-test-executable").hexdigest()
            with open(exe + ".sha256", "w", encoding="ascii") as stream:
                stream.write(f"{digest}  collector.exe\n")
            with patch.dict(os.environ, {"DCP_COLLECTOR_EXE_PATH": exe}):
                package = build_collector_package(
                    "customer-001", "collector-001", "claim" * 8,
                    "https://dashboard.example.com/",
                )
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            names = archive.namelist()
            activation = json.loads(
                archive.read("HawkHive-Collector/hawkhive-activation.json")
            )
        self.assertIn("HawkHive-Collector/HawkHive-DPC8001-Collector.exe", names)
        self.assertEqual(activation["cloud_url"], "https://dashboard.example.com")
        self.assertEqual(activation["activation_token"], "claim" * 8)
        self.assertNotIn("edge_token", activation)

    @patch("cloud_api.connect")
    def test_reusable_package_contains_customer_enrollment_but_no_upload_token(self, connect) -> None:
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = (3,)
        connect.return_value.__enter__.return_value = db
        with tempfile.TemporaryDirectory() as directory:
            exe = os.path.join(directory, "collector.exe")
            with open(exe, "wb") as stream:
                stream.write(b"MZ-test-executable")
            digest = hashlib.sha256(b"MZ-test-executable").hexdigest()
            with open(exe + ".sha256", "w", encoding="ascii") as stream:
                stream.write(f"{digest}  collector.exe\n")
            with patch.dict(os.environ, {
                "DCP_COLLECTOR_EXE_PATH": exe,
                "DCP_ACTIVATION_SECRET": self.ACTIVATION_SECRET,
            }):
                package = build_reusable_collector_package(
                    "customer-001", "https://dashboard.example.com/"
                )
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            names = archive.namelist()
            enrollment = json.loads(
                archive.read("HawkHive-Collector/hawkhive-enrollment.json")
            )
            instructions = archive.read("HawkHive-Collector/安装说明.txt").decode("utf-8-sig")
        self.assertIn("HawkHive-Collector/HawkHive-DPC8001-Collector.exe", names)
        self.assertEqual(enrollment["customer_id"], "customer-001")
        self.assertEqual(enrollment["cloud_url"], "https://dashboard.example.com")
        self.assertIn("enrollment_token", enrollment)
        self.assertNotIn("edge_token", enrollment)
        self.assertNotIn("activation_token", enrollment)
        self.assertIn("多台 Windows 电脑重复安装", instructions)

    def test_published_executable_requires_matching_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exe = os.path.join(directory, "collector.exe")
            with open(exe, "wb") as stream:
                stream.write(b"MZ-test-executable")
            with open(exe + ".sha256", "w", encoding="ascii") as stream:
                stream.write(f"{'0' * 64}  collector.exe\n")
            with patch.dict(os.environ, {"DCP_COLLECTOR_EXE_PATH": exe}):
                with self.assertRaises(HTTPException) as raised:
                    published_collector_executable()
        self.assertEqual(raised.exception.status_code, 503)


class CollectorHeartbeatValidationTests(unittest.TestCase):
    def payload(self) -> dict[str, object]:
        return {
            "site_id": "site-001", "collector_time": 1000.0,
            "version": "0.3.0", "hostname": "collector-a", "platform": "Windows",
            "monitor_running": True, "device_total": 2, "device_online": 1,
            "applied_config_version": 3, "config_apply_status": "applied",
        }

    def test_online_count_cannot_exceed_total(self) -> None:
        payload = self.payload()
        payload["device_online"] = 3
        with self.assertRaises(ValidationError):
            CollectorHeartbeat(**payload)

    def test_applied_status_requires_revision(self) -> None:
        payload = self.payload()
        payload["applied_config_version"] = None
        with self.assertRaises(ValidationError):
            CollectorHeartbeat(**payload)


class TopologyPermissionTests(unittest.TestCase):
    def test_edge_configuration_contains_only_its_site_devices(self) -> None:
        rooms = [{
            "id": "room-001", "name": "Workshop",
            "devices": [
                {"id": "device-a", "site_id": "site-a"},
                {"id": "device-b", "site_id": "site-b"},
            ],
        }]

        scoped = configuration_for_site(rooms, "site-a")

        self.assertEqual([item["id"] for item in scoped[0]["devices"]], ["device-a"])
        self.assertEqual(len(rooms[0]["devices"]), 2)

    def test_legacy_customer_and_explicit_admin_can_manage_topology(self) -> None:
        self.assertTrue(can_manage_topology({"role": "customer"}))
        self.assertTrue(can_manage_topology({"role": "customer_admin"}))
        self.assertTrue(can_manage_topology({"role": "admin"}))

    def test_read_only_user_cannot_manage_topology(self) -> None:
        self.assertFalse(can_manage_topology({"role": "viewer"}))
        with self.assertRaises(HTTPException) as raised:
            topology_manager({"role": "viewer"})
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
