"""Opt-in destructive-data acceptance against a disposable PostgreSQL database."""
from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

import cloud_api
from auth_service import AuthService


PASSWORD = "local-data-management-qa"


def seed_customer(label: str) -> dict[str, str]:
    customer_id = f"cleanupqa-{uuid4().hex}"
    user_id = str(uuid4())
    salt, digest = AuthService.password_hash(PASSWORD)
    site_id = f"{customer_id}-site"
    with cloud_api.connect() as db:
        db.execute("INSERT INTO customers(id,name) VALUES(%s,%s)", (customer_id, label))
        db.execute(
            """INSERT INTO customer_users(id,customer_id,username,display_name,password_salt,password_hash,created_at)
               VALUES(%s,%s,%s,%s,%s,%s,now()-interval '1 minute')""",
            (user_id, customer_id, customer_id, label, salt, digest),
        )
        db.execute("INSERT INTO sites(id,customer_id,name) VALUES(%s,%s,%s)", (site_id, customer_id, "Primary"))
    room_id = cloud_api.create_cloud_cleanroom(customer_id, "Cleanup room", actor_kind="customer_user", actor_user_id=user_id)
    device_id = cloud_api.create_cloud_device(customer_id, {
        "cleanroom_id": room_id, "site_id": site_id, "name": "Cleanup device",
        "host": "192.168.70.20", "tcp_port": 502, "slave": 1,
    }, actor_kind="customer_user", actor_user_id=user_id)
    return {"customer_id": customer_id, "user_id": user_id, "site_id": site_id, "room_id": room_id, "device_id": device_id}


def seed_monitoring(fixture: dict[str, str]) -> None:
    stamp = time.time()
    identity = {"customer_id": fixture["customer_id"], "site_id": fixture["site_id"]}
    base = dict(
        customer_id=fixture["customer_id"], site_id=fixture["site_id"],
        cleanroom_id=fixture["room_id"], cleanroom_name="Cleanup room",
        device_id=fixture["device_id"], device_name="Cleanup device",
        measured_at=stamp, source="device", particles={}, environment={}, alarm_status="NORMAL",
    )
    cloud_api.ingest_batch(cloud_api.ReadingBatch(site_id=fixture["site_id"], readings=[dict(base, record_uuid=uuid4())]), identity)
    cloud_api.ingest_latest(cloud_api.LatestBatch(site_id=fixture["site_id"], readings=[base]), identity)
    event_id = uuid4()
    cloud_api.ingest_alarms(cloud_api.AlarmBatch(site_id=fixture["site_id"], events=[dict(
        event_uuid=event_id, customer_id=fixture["customer_id"], site_id=fixture["site_id"],
        cleanroom_id=fixture["room_id"], device_id=fixture["device_id"], source="device",
        metric="temperature", started_at=stamp, limit_description="QA",
    )]), identity)
    with cloud_api.connect() as db:
        db.execute(
            """INSERT INTO email_notifications(id,customer_id,event_uuid,kind,recipient,payload,status)
               VALUES(%s,%s,%s,'alarm','qa@example.invalid','{}'::jsonb,'cancelled')""",
            (uuid4(), fixture["customer_id"], event_id),
        )


@unittest.skipUnless(os.environ.get("DCP_DATA_MANAGEMENT_TEST_DATABASE_URL"), "Opt-in isolated PostgreSQL required")
class DataManagementPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = os.environ["DCP_DATA_MANAGEMENT_TEST_DATABASE_URL"]
        if not dsn.endswith("/datamanagementqa"):
            raise RuntimeError("Use an isolated database named datamanagementqa")
        cls.environment = patch.dict(os.environ, {"DATABASE_URL": dsn, "DASHBOARD_SECURE_COOKIE": "0"})
        cls.environment.start()
        cloud_api.initialize_schema()

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()

    def setUp(self):
        self.fixture = seed_customer(self.id())
        self.client = TestClient(cloud_api.app, raise_server_exceptions=False)
        response = self.client.post("/api/login", json={"username": self.fixture["customer_id"], "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)

    def tearDown(self):
        self.client.close()

    def test_retired_cleanup_removes_dependents_and_keeps_audit(self):
        seed_monitoring(self.fixture)
        self.assertEqual(self.client.delete(f'/api/admin/devices/{self.fixture["device_id"]}').status_code, 200)
        preview = self.client.get("/api/admin/data-cleanup/preview").json()["data"]["retired_devices"]
        self.assertEqual((preview["devices"], preview["readings"], preview["alarms"], preview["notifications"]), (1, 1, 1, 1))
        self.assertEqual(self.client.post("/api/admin/data-cleanup", json={"scope": "retired_devices", "confirmation": "wrong"}).status_code, 400)
        response = self.client.post("/api/admin/data-cleanup", json={"scope": "retired_devices", "confirmation": "PURGE RETIRED"})
        self.assertEqual(response.status_code, 200, response.text)
        with cloud_api.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM devices WHERE id=%s", (self.fixture["device_id"],)).fetchone())
            for table in ("readings", "latest_readings", "alarm_events", "email_notifications", "device_assignment_periods"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table} WHERE customer_id=%s", (self.fixture["customer_id"],)).fetchone()[0], 0, table)
            audit = db.execute("SELECT details FROM configuration_audit_events WHERE customer_id=%s AND action='data_cleanup.retired_devices'", (self.fixture["customer_id"],)).fetchone()
            self.assertTrue(audit[0]["permanent"])

    def test_global_cleanup_retains_topology_and_accepts_new_data_afterward(self):
        seed_monitoring(self.fixture)
        response = self.client.post("/api/admin/data-cleanup", json={"scope": "all_monitoring", "confirmation": "PURGE ALL"})
        self.assertEqual(response.status_code, 200, response.text)
        with cloud_api.connect() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM devices WHERE id=%s AND enabled", (self.fixture["device_id"],)).fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM cleanrooms WHERE id=%s", (self.fixture["room_id"],)).fetchone())
            self.assertEqual(db.execute("SELECT count(*) FROM readings WHERE customer_id=%s", (self.fixture["customer_id"],)).fetchone()[0], 0)
        seed_monitoring(self.fixture)
        self.assertEqual(self.client.get("/api/stats").json()["data"]["readings"], 1)

    def test_workshop_delete_is_blocked_until_device_and_history_are_purged(self):
        seed_monitoring(self.fixture)
        url = f'/api/admin/cleanrooms/{self.fixture["room_id"]}?expected_name=Cleanup%20room'
        self.assertEqual(self.client.delete(url).status_code, 409)
        self.client.delete(f'/api/admin/devices/{self.fixture["device_id"]}')
        self.assertEqual(self.client.delete(url).status_code, 409)
        self.client.post("/api/admin/data-cleanup", json={"scope": "retired_devices", "confirmation": "PURGE RETIRED"})
        with cloud_api.connect() as db:
            db.execute(
                """INSERT INTO latest_readings(
                   customer_id,site_id,cleanroom_id,cleanroom_name,device_id,device_name,
                   measured_at,source,particles,environment,alarm_status
                   ) VALUES(%s,%s,%s,'Cleanup room',%s,'Retired orphan',now(),'device','{}','{}','NORMAL')""",
                (self.fixture["customer_id"], self.fixture["site_id"], self.fixture["room_id"], self.fixture["device_id"]),
            )
        self.assertEqual(self.client.delete(url).status_code, 409)
        with cloud_api.connect() as db:
            db.execute("DELETE FROM latest_readings WHERE customer_id=%s", (self.fixture["customer_id"],))
        self.assertEqual(self.client.delete(url.replace("Cleanup%20room", "Wrong")).status_code, 409)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"], [])

    def test_secondary_administrator_and_legacy_manager_cannot_purge(self):
        secondary_id = str(uuid4())
        salt, digest = AuthService.password_hash(PASSWORD)
        username = f'secondary-{uuid4().hex}'
        with cloud_api.connect() as db:
            db.execute(
                """INSERT INTO customer_users(id,customer_id,username,display_name,password_salt,password_hash,role)
                   VALUES(%s,%s,%s,'Secondary',%s,%s,'customer_admin')""",
                (secondary_id, self.fixture["customer_id"], username, salt, digest),
            )
        self.client.post("/api/login", json={"username": username, "password": PASSWORD})
        self.assertEqual(self.client.get("/api/admin/data-cleanup/preview").status_code, 403)
        with cloud_api.connect() as db:
            db.execute("UPDATE customer_users SET role='customer' WHERE id=%s", (secondary_id,))
        self.assertEqual(self.client.get("/api/admin/data-cleanup/preview").status_code, 403)

    def test_audit_failure_rolls_back_global_cleanup(self):
        seed_monitoring(self.fixture)
        with patch("cloud_api.record_configuration_audit", side_effect=RuntimeError("audit unavailable")):
            response = self.client.post("/api/admin/data-cleanup", json={"scope": "all_monitoring", "confirmation": "PURGE ALL"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.client.get("/api/stats").json()["data"]["readings"], 1)


if __name__ == "__main__":
    unittest.main()
