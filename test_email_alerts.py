from __future__ import annotations

import hashlib
import json
import os
import shutil
import smtplib
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

import cloud_api
from cloud_api import AlarmBatch, AlarmEventIn, app, ingest_alarms
from email_alerts import (
    build_message,
    claim_notification,
    deliver_one,
    finish_notification,
    record_worker_heartbeat,
    send_notification,
    smtp_config,
    validate_recipients,
)
from email_api import EmailSettings

SMTP_ENV = {
    "DCP_SMTP_HOST": "smtp.example.invalid",
    "DCP_SMTP_FROM": "alerts@example.com",
    "DCP_SMTP_SECURITY": "starttls",
    "DCP_SMTP_PORT": "587",
    "DCP_SMTP_USERNAME": "",
    "DCP_SMTP_PASSWORD": "",
}


def endpoint(path, method):
    return next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == path and method in route.methods
    )


class EmailUnitTests(unittest.TestCase):
    def test_rejects_nonfinite_values_and_recovery_before_start(self):
        valid = {
            "event_uuid": uuid4(),
            "customer_id": "c",
            "site_id": "s",
            "cleanroom_id": "r",
            "device_id": "d",
            "source": "device",
            "metric": "temperature",
            "started_at": 100,
            "limit_description": "18–25 °C",
        }
        for extra in (
            {"ended_at": 99},
            {"started_at": float("inf")},
            {"ended_at": float("nan")},
            {"trigger_value": float("nan")},
            {"peak_value": float("inf")},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                AlarmEventIn(**{**valid, **extra})

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL required for local TLS sink")
    @patch.dict(os.environ, SMTP_ENV)
    def test_real_smtp_transport_to_isolated_tls_sink(self):
        received, errors = [], []
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                    "-addext",
                    "subjectAltName=DNS:localhost",
                ],
                check=True,
                capture_output=True,
            )
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert, key)
            verified_client_context = ssl.create_default_context(cafile=str(cert))
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                listener.settimeout(5)

                def sink():
                    try:
                        connection, _ = listener.accept()
                        connection.settimeout(5)
                        with (
                            server_context.wrap_socket(
                                connection, server_side=True
                            ) as channel,
                            channel.makefile("rwb") as stream,
                        ):
                            stream.write(b"220 localhost QA sink\r\n")
                            stream.flush()
                            while line := stream.readline():
                                if line.upper().startswith(b"DATA"):
                                    stream.write(b"354 Send data\r\n")
                                    stream.flush()
                                    body = []
                                    while (part := stream.readline()) not in (
                                        b".\r\n",
                                        b"",
                                    ):
                                        body.append(part)
                                    received.append(b"".join(body))
                                stream.write(b"250 OK\r\n")
                                stream.flush()
                    except (OSError, ValueError) as error:
                        errors.append(error)

                thread = threading.Thread(target=sink, daemon=True)
                thread.start()
                with (
                    patch.dict(
                        os.environ,
                        {
                            "DCP_SMTP_HOST": "localhost",
                            "DCP_SMTP_SECURITY": "ssl",
                            "DCP_SMTP_PORT": str(listener.getsockname()[1]),
                        },
                    ),
                    patch(
                        "email_alerts.ssl.create_default_context",
                        return_value=verified_client_context,
                    ),
                ):
                    send_notification(
                        {
                            "id": uuid4(),
                            "kind": "test",
                            "recipient": "qa@example.com",
                            "payload": {},
                        }
                    )
                thread.join(timeout=5)
        self.assertFalse(errors)
        self.assertEqual(len(received), 1)
        self.assertIn(b"To: qa@example.com", received[0])
        self.assertIn(b"Message-ID:", received[0])

    def test_validation_and_deduplication(self):
        self.assertEqual(
            validate_recipients([" a+b@example.com ", "A+b@example.com"]),
            ["a+b@example.com"],
        )
        for bad in [
            "a@",
            "a..b@example.com",
            "a@example.com\r\nBcc:x@example.com",
            "a@-x.com",
            "a@localhost",
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_recipients([bad])
        for values in [
            {"recipients": ["bad"]},
            {"smtp_password": "forbidden"},
            {"recipients": [f"a{i}@example.com" for i in range(11)]},
        ]:
            with self.assertRaises(ValidationError):
                EmailSettings(**values)

    @patch.dict(os.environ, SMTP_ENV)
    def test_plaintext_smtp_rejected(self):
        with (
            patch.dict(os.environ, {"DCP_SMTP_SECURITY": "none"}),
            self.assertRaises(ValueError),
        ):
            smtp_config()

    @patch.dict(os.environ, SMTP_ENV)
    @patch("email_alerts.smtplib.SMTP")
    def test_tls_before_auth_private_recipient(self, smtp):
        client = smtp.return_value
        client.send_message.return_value = {}
        job = {
            "id": uuid4(),
            "kind": "test",
            "recipient": "qa@example.com",
            "payload": {},
        }
        with patch.dict(
            os.environ, {"DCP_SMTP_USERNAME": "tester", "DCP_SMTP_PASSWORD": "secret"}
        ):
            send_notification(job)
        names = [call[0] for call in client.method_calls]
        self.assertLess(names.index("starttls"), names.index("login"))
        self.assertLess(names.index("login"), names.index("send_message"))
        self.assertEqual(
            client.send_message.call_args.kwargs["to_addrs"], ["qa@example.com"]
        )
        self.assertIn("Test only", client.send_message.call_args.args[0].get_content())

    @patch.dict(os.environ, SMTP_ENV)
    @patch("email_alerts.smtplib.SMTP")
    def test_tls_failure_never_sends(self, smtp):
        smtp.return_value.starttls.side_effect = smtplib.SMTPNotSupportedError("no TLS")
        with self.assertRaises(smtplib.SMTPNotSupportedError):
            send_notification(
                {
                    "id": uuid4(),
                    "kind": "test",
                    "recipient": "qa@example.com",
                    "payload": {},
                }
            )
        smtp.return_value.send_message.assert_not_called()

    def test_message_id_stable_and_plain_text(self):
        job = {
            "id": uuid4(),
            "kind": "alarm",
            "recipient": "qa@example.com",
            "payload": {"room": "<script>unsafe</script>"},
        }
        a, b = [build_message(job, "alerts@example.com") for _ in range(2)]
        self.assertEqual(a["Message-ID"], b["Message-ID"])
        self.assertEqual(a.get_content_type(), "text/plain")


@unittest.skipUnless(
    os.environ.get("DCP_EMAIL_TEST_DATABASE_URL"), "Opt-in isolated PostgreSQL required"
)
class EmailPostgresTests(unittest.TestCase):
    def test_worker_readiness_is_separate_from_smtp_configuration(self):
        record_worker_heartbeat(cloud_api.connect)
        self.assertTrue(
            endpoint("/api/email-alerts", "GET")(self.user)["data"]["worker_available"]
        )
        with cloud_api.connect() as db:
            db.execute(
                "UPDATE email_worker_status SET heartbeat_at=now()-interval '10 minutes'"
            )
        data = endpoint("/api/email-alerts", "GET")(self.user)["data"]
        self.assertFalse(data["worker_available"])
        self.assertTrue(data["smtp_configured"])
        record_worker_heartbeat(cloud_api.connect)

    def test_history_includes_event_and_next_retry_for_traceability(self):
        self.settings()
        event = self.alarm()
        self.ingest(event)
        row = self.jobs()[0]
        self.assertEqual(row["event_uuid"], str(event.event_uuid))
        self.assertEqual(row["room"], "QA room")
        self.assertEqual(row["device"], "QA device")
        self.assertEqual(row["metric"], "temperature")
        self.assertIsNotNone(row["next_attempt_at"])

    @classmethod
    def setUpClass(cls):
        dsn = os.environ["DCP_EMAIL_TEST_DATABASE_URL"]
        if not dsn.endswith("/emailqa"):
            raise RuntimeError("Use an isolated database named emailqa")
        cls.environment = patch.dict(os.environ, {**SMTP_ENV, "DATABASE_URL": dsn})
        cls.environment.start()
        cloud_api.initialize_schema()
        cloud_api.initialize_schema()

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()

    def setUp(self):
        self.cid = f"emailqa-{uuid4()}"
        self.sid, self.rid, self.did = [str(uuid4()) for _ in range(3)]
        self.user = {
            "id": str(uuid4()),
            "customer_id": self.cid,
            "role": "customer_admin",
        }
        self.identity = {"customer_id": self.cid, "site_id": self.sid}
        with cloud_api.connect() as db:
            db.execute(
                "INSERT INTO customers(id,name) VALUES(%s,'Email QA')", (self.cid,)
            )
            db.execute(
                "INSERT INTO sites(id,customer_id,name) VALUES(%s,%s,'QA site')",
                (self.sid, self.cid),
            )
            db.execute(
                "INSERT INTO cleanrooms(id,customer_id,name) VALUES(%s,%s,'QA room')",
                (self.rid, self.cid),
            )
            db.execute(
                """INSERT INTO devices(id,customer_id,cleanroom_id,site_id,name,host)
                          VALUES(%s,%s,%s,%s,'QA device','192.168.1.2')""",
                (self.did, self.cid, self.rid, self.sid),
            )
            db.execute(
                """INSERT INTO customer_users(id,customer_id,username,display_name,password_salt,password_hash)
                          VALUES(%s,%s,%s,'QA admin','unused','unused')""",
                (self.user["id"], self.cid, self.cid),
            )

    def tearDown(self):
        # Preserve evidence; stop only this fixture's leftover work.
        with cloud_api.connect() as db:
            db.execute(
                "UPDATE email_notifications SET status='cancelled' WHERE customer_id=%s AND status IN ('pending','sending')",
                (self.cid,),
            )

    def settings(self, enabled=True, recipients=None, recovery=True):
        return endpoint("/api/email-alerts", "PUT")(
            EmailSettings(
                enabled=enabled,
                recipients=recipients if recipients is not None else ["qa@example.com"],
                notify_recovery=recovery,
            ),
            self.user,
        )

    def alarm(self, **overrides):
        values = {
            "event_uuid": uuid4(),
            "customer_id": self.cid,
            "site_id": self.sid,
            "cleanroom_id": self.rid,
            "device_id": self.did,
            "source": "device",
            "metric": "temperature",
            "started_at": time.time(),
            "trigger_value": 30,
            "peak_value": 31,
            "limit_description": "18–25 °C",
        }
        values.update(overrides)
        return AlarmEventIn(**values)

    def ingest(self, event):
        return ingest_alarms(
            AlarmBatch(site_id=self.sid, events=[event]), self.identity
        )

    def jobs(self):
        return endpoint("/api/email-alerts/history", "GET")(self.user)["data"]

    def test_default_off_and_old_events_suppressed(self):
        self.assertFalse(
            endpoint("/api/email-alerts", "GET")(self.user)["data"]["enabled"]
        )
        old = self.alarm(started_at=time.time() - 1000)
        self.ingest(old)
        self.settings()
        self.ingest(old)
        self.assertEqual(self.jobs(), [])
        with self.assertRaises(ValidationError):
            self.alarm(source="demo")

    def test_duplicate_recovery_and_out_of_order(self):
        self.settings()
        event = self.alarm()
        self.ingest(event)
        self.ingest(event)
        self.assertEqual(len(self.jobs()), 1)
        sent = []
        self.assertTrue(deliver_one(cloud_api.connect, sent.append))
        recovered = event.model_copy(update={"ended_at": time.time()})
        self.ingest(recovered)
        self.ingest(recovered)
        self.ingest(event)
        self.assertTrue(deliver_one(cloud_api.connect, sent.append))
        self.assertEqual({j["kind"] for j in sent}, {"alarm", "recovery"})
        self.assertEqual(len(sent), 2)
        with cloud_api.connect() as db:
            self.assertIsNotNone(
                db.execute(
                    "SELECT ended_at FROM alarm_events WHERE event_uuid=%s",
                    (event.event_uuid,),
                ).fetchone()[0]
            )

    def test_recovery_cancels_unsent_active_notice(self):
        self.settings()
        event = self.alarm()
        self.ingest(event)
        self.ingest(event.model_copy(update={"ended_at": time.time()}))
        self.assertEqual(
            {j["kind"]: j["status"] for j in self.jobs()},
            {"alarm": "cancelled", "recovery": "pending"},
        )

    def test_recovery_opt_out_cancels_stale_alarm(self):
        self.settings(recovery=False)
        event = self.alarm()
        self.ingest(event)
        self.ingest(event.model_copy(update={"ended_at": time.time()}))
        self.assertEqual(self.jobs()[0]["status"], "cancelled")
        send = MagicMock()
        deliver_one(cloud_api.connect, send)
        send.assert_not_called()
        self.assertEqual(self.jobs()[0]["status"], "cancelled")

    def test_disable_and_remove_recipient(self):
        self.settings(recipients=["qa@example.com", "other@example.com"])
        self.ingest(self.alarm())
        self.settings(recipients=["qa@example.com"])
        self.assertEqual(
            {j["recipient"]: j["status"] for j in self.jobs()},
            {"qa@example.com": "pending", "other@example.com": "cancelled"},
        )
        self.settings(enabled=False)
        self.assertEqual({j["status"] for j in self.jobs()}, {"cancelled"})

    def test_backoff_failure_cap_and_secret_redaction(self):
        self.settings()
        self.ingest(self.alarm())
        sender = MagicMock(
            side_effect=smtplib.SMTPAuthenticationError(535, b"SECRET_PASSWORD")
        )
        for attempt in range(1, 6):
            self.assertTrue(deliver_one(cloud_api.connect, sender))
            job = self.jobs()[0]
            self.assertEqual(job["attempts"], attempt)
            self.assertNotIn("SECRET_PASSWORD", json.dumps(job))
            if attempt < 5:
                self.assertFalse(deliver_one(cloud_api.connect, sender))
                with cloud_api.connect() as db:
                    db.execute(
                        "UPDATE email_notifications SET next_attempt_at=now() WHERE customer_id=%s",
                        (self.cid,),
                    )
        self.assertEqual(job["status"], "failed")
        self.assertFalse(deliver_one(cloud_api.connect, sender))

    def test_concurrent_workers_send_each_recipient_once(self):
        self.settings(recipients=["qa@example.com", "other@example.com"])
        self.ingest(self.alarm())
        sent = []

        def sender(job):
            time.sleep(0.05)
            sent.append(job["recipient"])

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: deliver_one(cloud_api.connect, sender), range(4)))
        self.assertCountEqual(sent, ["qa@example.com", "other@example.com"])

    def test_slow_smtp_does_not_hold_database_transaction_or_block_recovery(self):
        self.settings()
        event = self.alarm()
        self.ingest(event)
        entered, release = threading.Event(), threading.Event()

        def sender(_job):
            entered.set()
            self.assertTrue(release.wait(5))

        with ThreadPoolExecutor(max_workers=2) as pool:
            delivery = pool.submit(deliver_one, cloud_api.connect, sender)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.jobs()[0]["status"], "sending")
                recovery = pool.submit(
                    self.ingest, event.model_copy(update={"ended_at": time.time()})
                )
                recovery.result(timeout=1)
                self.settings(enabled=False)
                with cloud_api.connect() as db:
                    db.execute("SET LOCAL lock_timeout='200ms'")
                    db.execute(
                        "SELECT id FROM email_notifications WHERE customer_id=%s FOR UPDATE",
                        (self.cid,),
                    )
            finally:
                release.set()
            self.assertTrue(delivery.result(timeout=2))

    def test_claim_restart_recovery_fences_stale_worker_and_counts_attempts(self):
        self.settings()
        self.ingest(self.alarm())
        first = claim_notification(cloud_api.connect)
        self.assertIsNone(claim_notification(cloud_api.connect))
        with cloud_api.connect() as db:
            db.execute(
                "UPDATE email_notifications SET lease_until=now()-interval '1 second' WHERE id=%s",
                (first["id"],),
            )
        second = claim_notification(cloud_api.connect)
        self.assertEqual(second["attempts"], 2)
        self.assertNotEqual(first["claim_token"], second["claim_token"])
        self.assertFalse(finish_notification(cloud_api.connect, first))
        self.assertTrue(finish_notification(cloud_api.connect, second))
        self.assertEqual(self.jobs()[0]["status"], "sent")

    def test_expired_final_attempt_stops_with_unknown_delivery(self):
        self.settings()
        self.ingest(self.alarm())
        job = claim_notification(cloud_api.connect)
        with cloud_api.connect() as db:
            db.execute(
                """UPDATE email_notifications SET attempts=5,lease_until=now()-interval '1 second'
                          WHERE id=%s""",
                (job["id"],),
            )
        sender = MagicMock()
        self.assertTrue(deliver_one(cloud_api.connect, sender))
        sender.assert_not_called()
        self.assertEqual(self.jobs()[0]["last_error"], "DeliveryOutcomeUnknown")
        self.assertEqual(self.jobs()[0]["status"], "failed")

    def test_disabling_expired_inflight_job_does_not_claim_it_was_never_sent(self):
        self.settings()
        self.ingest(self.alarm())
        job = claim_notification(cloud_api.connect)
        self.settings(enabled=False)
        with cloud_api.connect() as db:
            db.execute(
                "UPDATE email_notifications SET lease_until=now()-interval '1 second' WHERE id=%s",
                (job["id"],),
            )
        sender = MagicMock()
        self.assertTrue(deliver_one(cloud_api.connect, sender))
        sender.assert_not_called()
        self.assertEqual(self.jobs()[0]["last_error"], "DeliveryOutcomeUnknown")
        self.assertFalse(finish_notification(cloud_api.connect, job))

    def test_failed_recipient_does_not_retry_successful_recipient(self):
        self.settings(recipients=["good@example.com", "bad@example.com"])
        self.ingest(self.alarm())

        def sender(job):
            if job["recipient"] == "bad@example.com":
                raise smtplib.SMTPRecipientsRefused(
                    {"bad@example.com": (550, "rejected")}
                )

        deliver_one(cloud_api.connect, sender)
        deliver_one(cloud_api.connect, sender)
        self.assertEqual(
            {job["recipient"]: job["status"] for job in self.jobs()},
            {"good@example.com": "sent", "bad@example.com": "pending"},
        )

    def test_failure_after_disable_does_not_requeue_cancelled_work(self):
        self.settings()
        self.ingest(self.alarm())
        job = claim_notification(cloud_api.connect)
        self.settings(enabled=False)
        self.assertTrue(
            finish_notification(cloud_api.connect, job, "SMTPAuthenticationError")
        )
        self.assertEqual(self.jobs()[0]["status"], "cancelled")

    def test_poison_payload_records_failure_instead_of_crashing_worker(self):
        self.settings()
        self.ingest(self.alarm())
        self.assertTrue(
            deliver_one(
                cloud_api.connect, MagicMock(side_effect=KeyError("private payload"))
            )
        )
        self.assertEqual(self.jobs()[0]["last_error"], "KeyError")
        self.assertEqual(self.jobs()[0]["attempts"], 1)

    def test_test_email_requires_recipients_and_rate_limit(self):
        test = endpoint("/api/email-alerts/test", "POST")
        with self.assertRaises(HTTPException):
            test(self.user)
        self.settings(enabled=False)
        self.assertEqual(test(self.user)["data"]["queued"], 1)
        with self.assertRaises(HTTPException) as error:
            test(self.user)
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(self.jobs()[0]["kind"], "test")

    def test_smtp_and_empty_recipient_gates(self):
        with patch.dict(os.environ, {"DCP_SMTP_HOST": ""}):
            with self.assertRaises(HTTPException):
                self.settings()
            result = self.settings(enabled=False)
            self.assertFalse(result["data"]["smtp_configured"])
        self.assertNotIn("password", json.dumps(result))
        with self.assertRaises(HTTPException):
            self.settings(recipients=[])

    def test_batch_failure_rolls_back_event_and_queue(self):
        self.settings()
        valid = self.alarm()
        invalid = self.alarm(customer_id="another-customer")
        with self.assertRaises(HTTPException):
            ingest_alarms(
                AlarmBatch(site_id=self.sid, events=[valid, invalid]), self.identity
            )
        self.assertEqual(self.jobs(), [])
        with cloud_api.connect() as db:
            self.assertIsNone(
                db.execute(
                    "SELECT event_uuid FROM alarm_events WHERE event_uuid=%s",
                    (valid.event_uuid,),
                ).fetchone()
            )

    def test_http_auth_roles_tenant_and_input_boundaries(self):
        import uvicorn

        self.settings()
        self.ingest(self.alarm())
        token = str(uuid4())
        with cloud_api.connect() as db:
            db.execute(
                "INSERT INTO customer_sessions(token_hash,user_id,expires_at) VALUES(%s,%s,now()+interval '1 hour')",
                (hashlib.sha256(token.encode()).hexdigest(), self.user["id"]),
            )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.01)

            def request(path, method="GET", payload=None, authenticated=True):
                headers = {"Content-Type": "application/json"}
                if authenticated:
                    headers["Cookie"] = f"dcp_session={token}"
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    headers=headers,
                    method=method,
                    data=json.dumps(payload).encode() if payload is not None else None,
                )
                try:
                    with urllib.request.urlopen(req, timeout=3) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)

            self.assertEqual(request("/api/email-alerts", authenticated=False)[0], 401)
            self.assertEqual(request("/api/email-alerts")[0], 200)
            self.assertEqual(
                request(
                    "/api/email-alerts", "PUT", {"enabled": True, "recipients": ["bad"]}
                )[0],
                422,
            )
            self.assertEqual(
                request("/api/email-alerts", "PUT", {"customer_id": "foreign"})[0], 422
            )
            self.assertEqual(
                len(
                    request("/api/email-alerts/history?customer_id=foreign")[1]["data"]
                ),
                1,
            )
            with cloud_api.connect() as db:
                db.execute(
                    "UPDATE customer_users SET role='viewer' WHERE id=%s",
                    (self.user["id"],),
                )
            for path, method in [
                ("/api/email-alerts", "GET"),
                ("/api/email-alerts", "PUT"),
                ("/api/email-alerts/history", "GET"),
                ("/api/email-alerts/test", "POST"),
            ]:
                self.assertEqual(
                    request(path, method, {} if method == "PUT" else None)[0], 403
                )
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            sock.close()


if __name__ == "__main__":
    unittest.main()
