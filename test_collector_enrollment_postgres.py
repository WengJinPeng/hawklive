from __future__ import annotations

import hashlib
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException

import cloud_api


@unittest.skipUnless(
    os.environ.get("DCP_ENROLLMENT_TEST_DATABASE_URL"),
    "Opt-in isolated PostgreSQL required",
)
class CollectorEnrollmentPostgresTests(unittest.TestCase):
    ACTIVATION_SECRET = "cd" * 32

    @classmethod
    def setUpClass(cls) -> None:
        dsn = os.environ["DCP_ENROLLMENT_TEST_DATABASE_URL"]
        if not dsn.endswith("/enrollmentqa"):
            raise RuntimeError("Use an isolated database named enrollmentqa")
        cls.environment = patch.dict(os.environ, {
            "DATABASE_URL": dsn,
            "DCP_ACTIVATION_SECRET": cls.ACTIVATION_SECRET,
        })
        cls.environment.start()
        cloud_api.initialize_schema()
        cloud_api.initialize_schema()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.environment.stop()

    def test_pending_approval_and_per_machine_credential_flow(self) -> None:
        customer_id = f"enrollmentqa-{uuid4()}"
        user_id = uuid4()
        machine_id = uuid4().hex
        with cloud_api.connect() as db:
            db.execute(
                "INSERT INTO customers(id,name) VALUES(%s,'Enrollment QA')",
                (customer_id,),
            )
            db.execute(
                """
                INSERT INTO customer_users(
                    id,customer_id,username,display_name,password_salt,password_hash
                ) VALUES(%s,%s,%s,'QA admin','unused','unused')
                """,
                (user_id, customer_id, customer_id),
            )
        token = cloud_api.customer_enrollment_token(customer_id, 1)
        pending = cloud_api.register_collector_enrollment(
            customer_id, token, machine_id, "claim-secret-" * 3,
            "WIN-QA-01", "Windows 11",
        )
        retry = cloud_api.register_collector_enrollment(
            customer_id, token, machine_id, "claim-secret-" * 3,
            "WIN-QA-01", "Windows 11",
        )
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(retry["status"], "pending")
        self.assertNotIn("pairing_code", pending)
        self.assertNotIn("pairing_code", retry)
        inbox = cloud_api.admin_pending_collectors({
            "id": str(user_id), "customer_id": customer_id, "role": "customer_admin",
        })["data"]
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["hostname"], "WIN-QA-01")
        self.assertNotIn("pairing_code", inbox[0])
        enrollment_id = str(inbox[0]["id"])
        approved = cloud_api.approve_collector_enrollment(
            customer_id, enrollment_id, "一楼采集器", actor_user_id=str(user_id)
        )
        credential = cloud_api.register_collector_enrollment(
            customer_id, token, machine_id, "claim-secret-" * 3,
            "WIN-QA-01", "Windows 11",
        )
        self.assertEqual(credential["status"], "approved")
        self.assertEqual(credential["site_id"], approved["id"])
        self.assertGreaterEqual(len(str(credential["token"])), 32)
        with cloud_api.connect() as db:
            token_row = db.execute(
                "SELECT customer_id,site_id FROM edge_tokens WHERE token_hash=%s",
                (hashlib.sha256(str(credential["token"]).encode()).hexdigest(),),
            ).fetchone()
        self.assertEqual(tuple(map(str, token_row)), (customer_id, str(approved["id"])))
        with self.assertRaises(HTTPException) as raised:
            cloud_api.register_collector_enrollment(
                customer_id, token, machine_id, "different-secret-" * 3,
                "WIN-QA-01", "Windows 11",
            )
        self.assertEqual(raised.exception.status_code, 401)
        generation = cloud_api.rotate_customer_enrollment(
            customer_id, actor_user_id=str(user_id)
        )
        self.assertEqual(generation, 2)
        with self.assertRaises(HTTPException) as rotated:
            cloud_api.register_collector_enrollment(
                customer_id, token, uuid4().hex, "third-machine-secret-" * 2,
                "WIN-QA-03", "Windows 11",
            )
        self.assertEqual(rotated.exception.status_code, 401)
        with cloud_api.connect() as db:
            still_enabled = db.execute(
                "SELECT enabled FROM edge_tokens WHERE token_hash=%s",
                (hashlib.sha256(str(credential["token"]).encode()).hexdigest(),),
            ).fetchone()
        self.assertEqual(still_enabled, (True,))

    def test_rejected_machine_can_reapply_only_after_package_rotation(self) -> None:
        customer_id = f"enrollmentretry-{uuid4()}"
        user_id = uuid4()
        machine_id = uuid4().hex
        claim_secret = "retry-claim-secret-" * 2
        with cloud_api.connect() as db:
            db.execute(
                "INSERT INTO customers(id,name) VALUES(%s,'Enrollment retry QA')",
                (customer_id,),
            )
            db.execute(
                """
                INSERT INTO customer_users(
                    id,customer_id,username,display_name,password_salt,password_hash
                ) VALUES(%s,%s,%s,'QA admin','unused','unused')
                """,
                (user_id, customer_id, customer_id),
            )

        old_token = cloud_api.customer_enrollment_token(customer_id, 1)
        pending = cloud_api.register_collector_enrollment(
            customer_id, old_token, machine_id, claim_secret,
            "WIN-RETRY-01", "Windows 11",
        )
        inbox = cloud_api.admin_pending_collectors({
            "id": str(user_id), "customer_id": customer_id, "role": "customer_admin",
        })["data"]
        enrollment_id = str(inbox[0]["id"])
        cloud_api.revoke_collector_enrollment(
            customer_id, enrollment_id, actor_user_id=str(user_id)
        )
        with self.assertRaises(HTTPException) as rejected:
            cloud_api.register_collector_enrollment(
                customer_id, old_token, machine_id, claim_secret,
                "WIN-RETRY-01", "Windows 11",
            )
        self.assertEqual(rejected.exception.status_code, 403)

        generation = cloud_api.rotate_customer_enrollment(
            customer_id, actor_user_id=str(user_id)
        )
        self.assertEqual(generation, 2)
        with self.assertRaises(HTTPException) as stale_package:
            cloud_api.register_collector_enrollment(
                customer_id, old_token, machine_id, claim_secret,
                "WIN-RETRY-01", "Windows 11",
            )
        self.assertEqual(stale_package.exception.status_code, 401)

        new_token = cloud_api.customer_enrollment_token(customer_id, generation)
        reapplied = cloud_api.register_collector_enrollment(
            customer_id, new_token, machine_id, claim_secret,
            "WIN-RETRY-01", "Windows 11",
        )
        self.assertEqual(reapplied["status"], "pending")
        self.assertNotIn("pairing_code", pending)
        self.assertNotIn("pairing_code", reapplied)
        refreshed = cloud_api.admin_pending_collectors({
            "id": str(user_id), "customer_id": customer_id, "role": "customer_admin",
        })["data"]
        self.assertEqual([str(item["id"]) for item in refreshed], [enrollment_id])


if __name__ == "__main__":
    unittest.main()
