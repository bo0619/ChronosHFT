import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from infrastructure.external_alerts import (
    AlertDeliveryError,
    ExternalAlertService,
    HttpsWebhookTransport,
    redact_alert_text,
)


class FakeTransport:
    def __init__(self, outcomes=None, *, block_call: int | None = None):
        self.outcomes = list(outcomes or [])
        self.block_call = block_call
        self.release = threading.Event()
        self.changed = threading.Condition()
        self.calls = []
        self.thread_ids = []
        self.closed = False

    def send(
        self,
        payload,
        *,
        connect_timeout_sec,
        read_timeout_sec,
    ):
        with self.changed:
            self.calls.append(dict(payload))
            self.thread_ids.append(threading.get_ident())
            call_number = len(self.calls)
            self.changed.notify_all()
        if self.block_call == call_number:
            self.release.wait(timeout=2.0)
        outcome = (
            self.outcomes[call_number - 1]
            if call_number <= len(self.outcomes)
            else None
        )
        if isinstance(outcome, BaseException):
            raise outcome

    def wait_for_calls(self, count: int, timeout_sec: float = 1.0) -> bool:
        with self.changed:
            return self.changed.wait_for(
                lambda: len(self.calls) >= count,
                timeout=timeout_sec,
            )

    def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.closed = False

    @property
    def text(self):
        raise AssertionError("response body must not be read")

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.closed = False
        self.trust_env = True

    def post(self, endpoint, **kwargs):
        self.calls.append((endpoint, dict(kwargs)))
        if self.error is not None:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


def alert_config(spool_path: Path, *, queue_capacity: int = 8):
    return {
        "live_launch": {
            "deployment_id": "canary-alert-test-001",
            "stage": "canary",
        },
        "alert": {
            "active": True,
            "transport": "https_webhook",
            "webhook_url_env": "CHRONOSHFT_ALERT_WEBHOOK_URL",
            "minimum_level": "WARNING",
            "queue_capacity": queue_capacity,
            "connect_timeout_sec": 0.01,
            "read_timeout_sec": 0.01,
            "max_attempts": 1,
            "retry_backoff_sec": 0.0,
            "startup_probe_required": True,
            "startup_probe_timeout_sec": 1.0,
            "runtime_fail_closed": True,
            "recovery_probe_interval_sec": 10.0,
            "shutdown_flush_timeout_sec": 1.0,
            "failure_spool_path": str(spool_path),
            "failure_spool_fsync": True,
        },
    }


class ExternalAlertServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _spool_path(self):
        return Path(self.temp_dir.name) / "external_alert_failures.jsonl"

    def test_startup_probe_and_logger_warning_use_worker_thread(self):
        spool = self._spool_path()
        transport = FakeTransport()
        service = ExternalAlertService(
            alert_config(spool),
            transport=transport,
        )
        self.assertTrue(service.start())
        self.assertTrue(service.probe_startup())
        self.assertFalse(service.enqueue_log("INFO", "not external"))
        self.assertTrue(service.enqueue_log("WARNING", "risk warning"))
        self.assertTrue(service.wait_until_idle(1.0))

        snapshot = service.get_health_snapshot()
        self.assertTrue(snapshot["healthy"])
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(
            all(
                thread_id != threading.get_ident()
                for thread_id in transport.thread_ids
            )
        )
        self.assertTrue(service.stop())
        self.assertTrue(transport.closed)

    def test_failure_record_contains_no_url_response_or_exception_text(self):
        spool = self._spool_path()
        secret = "credential-bearing-hook-value"
        transport = FakeTransport(
            [
                None,
                RuntimeError(
                    f"https://alerts.invalid/{secret} response-body-secret"
                ),
            ]
        )
        service = ExternalAlertService(
            alert_config(spool),
            transport=transport,
        )
        service.start()
        self.assertTrue(service.probe_startup())
        self.assertTrue(
            service.enqueue(
                level="ERROR",
                source="test",
                code="delivery_failure",
                message=(
                    f"failed endpoint=https://alerts.invalid/{secret} "
                    "api_secret=do-not-store"
                ),
            )
        )
        self.assertTrue(service.wait_until_idle(1.0))
        snapshot = service.get_health_snapshot()
        self.assertFalse(snapshot["healthy"])
        self.assertEqual(snapshot["last_failure_kind"], "transport_exception")
        self.assertTrue(service.stop())

        rendered = spool.read_text(encoding="utf-8")
        self.assertNotIn("https://", rendered)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("response-body-secret", rendered)
        self.assertNotIn("do-not-store", rendered)
        record = json.loads(rendered.strip())
        self.assertEqual(record["record_type"], "delivery_failure")
        self.assertEqual(record["failure_kind"], "transport_exception")
        self.assertNotIn("exception", record)
        self.assertNotIn("response_body", record)

    def test_blocked_transport_does_not_block_or_unbound_producer(self):
        spool = self._spool_path()
        transport = FakeTransport(block_call=2)
        service = ExternalAlertService(
            alert_config(spool, queue_capacity=2),
            transport=transport,
        )
        service.start()
        self.assertTrue(service.probe_startup())
        self.assertTrue(service.enqueue_log("ERROR", "blocking delivery"))
        self.assertTrue(transport.wait_for_calls(2))

        producer_result = []

        def produce():
            producer_result.extend(
                service.enqueue_log("WARNING", f"queued-{index}")
                for index in range(4)
            )

        producer = threading.Thread(target=produce)
        producer.start()
        producer.join(timeout=0.25)
        self.assertFalse(producer.is_alive())
        self.assertIn(False, producer_result)

        transport.release.set()
        self.assertTrue(service.wait_until_idle(1.0))
        snapshot = service.get_health_snapshot()
        self.assertGreater(snapshot["dropped_alerts"], 0)
        self.assertTrue(service.stop())

        records = [
            json.loads(line)
            for line in spool.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(
            any(
                record.get("record_type") == "queue_overflow_summary"
                for record in records
            )
        )

    def test_retry_budget_is_bounded_and_probe_fails_closed(self):
        spool = self._spool_path()
        transport = FakeTransport(
            [
                AlertDeliveryError("network", retryable=True),
                AlertDeliveryError("network", retryable=True),
                AlertDeliveryError("network", retryable=True),
                None,
            ]
        )
        config = alert_config(spool)
        config["alert"]["max_attempts"] = 3
        service = ExternalAlertService(config, transport=transport)
        service.start()

        self.assertFalse(service.probe_startup())
        self.assertEqual(len(transport.calls), 3)
        self.assertFalse(service.get_health_snapshot()["healthy"])
        self.assertTrue(service.stop())

    def test_event_payload_and_snapshot_are_bounded_and_secret_free(self):
        spool = self._spool_path()
        transport = FakeTransport()
        service = ExternalAlertService(
            alert_config(spool),
            transport=transport,
            event_id_factory=lambda: "event-001",
        )
        service.start()
        self.assertTrue(service.probe_startup())
        self.assertTrue(
            service.enqueue_event(
                {
                    "level": "CRITICAL",
                    "source": "risk",
                    "code": "kill",
                    "message": (
                        "Authorization: Bearer secret "
                        "https://alerts.invalid/private"
                    ),
                }
            )
        )
        self.assertTrue(service.wait_until_idle(1.0))
        snapshot = service.get_health_snapshot()
        self.assertNotIn("url", json.dumps(snapshot).lower())
        payload = transport.calls[-1]
        rendered = json.dumps(payload)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("https://", rendered)
        self.assertTrue(service.stop())

    def test_https_transport_does_not_read_body_or_render_endpoint(self):
        response = FakeResponse(204)
        session = FakeSession(response=response)
        transport = HttpsWebhookTransport(
            "https://alerts.invalid/credential",
            session=session,
        )

        transport.send(
            {"message": "test"},
            connect_timeout_sec=1.0,
            read_timeout_sec=2.0,
        )

        self.assertEqual(
            repr(transport),
            "HttpsWebhookTransport(endpoint=<redacted>)",
        )
        self.assertTrue(response.closed)
        _endpoint, kwargs = session.calls[0]
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["timeout"], (1.0, 2.0))
        self.assertFalse(session.trust_env)
        transport.close()
        self.assertTrue(session.closed)

    def test_redactor_removes_urls_and_common_credentials(self):
        rendered = redact_alert_text(
            "url=https://alerts.invalid/private "
            "api_key=alpha token=beta Authorization: Bearer gamma"
        )
        self.assertNotIn("https://", rendered)
        self.assertNotIn("alpha", rendered)
        self.assertNotIn("beta", rendered)
        self.assertNotIn("gamma", rendered)

if __name__ == "__main__":
    unittest.main()
