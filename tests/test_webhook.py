"""Execution-status notifications: the engine index, the executions endpoint,
and webhook delivery over the dedicated worker thread."""
import contextlib
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from exchange.api import create_app
from exchange.domain import Side
from exchange.webhook import WebhookDelivery, WebhookDispatcher

from tests.harness import EngineHarness


def payload(**overrides) -> dict:
    base = {
        "broker_id": "broker-1",
        "document": "111.111.111-11",
        "side": "ask",
        "symbol": "AAPL",
        "price": "10.00",
        "quantity": 1000,
        "valid_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    base.update(overrides)
    return base


class Recorder:
    """A sender that captures deliveries instead of doing HTTP, so tests can
    exercise the real worker thread without a live endpoint."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.deliveries: list[WebhookDelivery] = []

    def __call__(self, delivery: WebhookDelivery) -> None:
        with self._lock:
            self.deliveries.append(delivery)

    def wait_for(self, count: int, timeout: float = 2.0) -> list[WebhookDelivery]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.deliveries) >= count:
                    break
            time.sleep(0.005)
        with self._lock:
            return list(self.deliveries)


class ClientTradesTests(unittest.TestCase):
    """The engine index that backs both the endpoint and the webhook."""

    def setUp(self) -> None:
        self.h = self.enterContext(_closing(EngineHarness()))

    def test_client_trades_indexes_both_counterparties(self) -> None:
        maker = self.h.sell("10.00", 100, broker_id="mm")
        taker = self.h.buy("10.00", 100, broker_id="broker-1")

        self.assertEqual(self.h.engine.client_trades("broker-1"), self.h.engine.trades)
        self.assertEqual(self.h.engine.client_trades("mm"), self.h.engine.trades)
        self.assertEqual(taker.filled_quantity, 100)
        self.assertEqual(maker.filled_quantity, 100)

    def test_client_trades_is_empty_for_unknown_broker(self) -> None:
        self.h.sell("10.00", 100)
        self.assertEqual(self.h.engine.client_trades("nobody"), [])

    def test_self_trade_records_once_per_broker(self) -> None:
        # Same broker on both sides: the shared trade is indexed once, not twice.
        self.h.sell("10.00", 100, broker_id="solo")
        self.h.buy("10.00", 100, broker_id="solo")
        self.assertEqual(len(self.h.engine.client_trades("solo")), 1)


class ExecutionsEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = self.enterContext(TestClient(create_app(":memory:")))

    def test_reports_cumulative_fill_per_order(self) -> None:
        resting = self.client.post(
            "/orders", json=payload(side="ask", quantity=1000, broker_id="mm")
        ).json()
        # Two partial hits against the same resting ask.
        self.client.post("/orders", json=payload(side="bid", quantity=300, broker_id="taker"))
        self.client.post("/orders", json=payload(side="bid", quantity=200, broker_id="taker"))

        reports = self.client.get("/clients/mm/executions").json()
        self.assertEqual(
            reports,
            [{"order_id": resting["id"], "price": "10.00", "quantity": 500}],
        )

    def test_empty_for_broker_without_fills(self) -> None:
        self.client.post("/orders", json=payload())  # rests, never trades
        self.assertEqual(self.client.get("/clients/broker-1/executions").json(), [])


class WebhookDeliveryTests(unittest.TestCase):
    def _app(self, recorder: Recorder):
        dispatcher = WebhookDispatcher(sender=recorder, max_attempts=1)
        return TestClient(create_app(":memory:", dispatcher=dispatcher))

    def test_fill_pushes_cumulative_report_to_both_brokers(self) -> None:
        recorder = Recorder()
        with self._app(recorder) as client:
            client.put("/clients/mm/webhook", json={"url": "https://mm.example/hook"})
            client.put("/clients/taker/webhook", json={"url": "https://taker.example/hook"})

            ask = client.post(
                "/orders", json=payload(side="ask", quantity=100, broker_id="mm")
            ).json()
            bid = client.post(
                "/orders", json=payload(side="bid", quantity=100, broker_id="taker")
            ).json()

            deliveries = recorder.wait_for(2)

        by_key = {d.idempotency_key: d for d in deliveries}
        self.assertEqual(set(by_key), {ask["id"], bid["id"]})
        self.assertEqual(by_key[ask["id"]].url, "https://mm.example/hook")
        self.assertEqual(
            by_key[ask["id"]].payload,
            {"order_id": ask["id"], "price": "10.00", "quantity": 100},
        )

    def test_idempotency_key_is_the_order_id(self) -> None:
        recorder = Recorder()
        with self._app(recorder) as client:
            client.put("/clients/mm/webhook", json={"url": "https://mm.example/hook"})
            # Resting order filled in two steps: both pushes carry the same key,
            # the cumulative quantity grows — that is what makes order_id a valid
            # idempotency key (last-write-wins on the subscriber).
            ask = client.post(
                "/orders", json=payload(side="ask", quantity=1000, broker_id="mm")
            ).json()
            client.post("/orders", json=payload(side="bid", quantity=300))
            client.post("/orders", json=payload(side="bid", quantity=200))

            deliveries = recorder.wait_for(2)

        mine = [d for d in deliveries if d.idempotency_key == ask["id"]]
        self.assertEqual([d.payload["quantity"] for d in mine], [300, 500])

    def test_resting_order_without_fill_pushes_nothing(self) -> None:
        recorder = Recorder()
        with self._app(recorder) as client:
            client.put("/clients/broker-1/webhook", json={"url": "https://b1.example/hook"})
            client.post("/orders", json=payload())  # rests, no counterparty

            self.assertEqual(recorder.wait_for(1, timeout=0.3), [])

    def test_unsubscribed_broker_is_not_notified(self) -> None:
        recorder = Recorder()
        with self._app(recorder) as client:
            client.put("/clients/mm/webhook", json={"url": "https://mm.example/hook"})
            client.post("/orders", json=payload(side="ask", quantity=100, broker_id="mm"))
            # taker never registered a webhook: only mm should be delivered to.
            client.post("/orders", json=payload(side="bid", quantity=100, broker_id="taker"))

            deliveries = recorder.wait_for(2, timeout=0.5)

        self.assertEqual([d.url for d in deliveries], ["https://mm.example/hook"])


class ReplayDoesNotNotifyTests(unittest.TestCase):
    def test_restart_replays_without_refiring_webhooks(self) -> None:
        db = self.enterContext(_temp_db())

        first = Recorder()
        with TestClient(
            create_app(db, dispatcher=WebhookDispatcher(sender=first, max_attempts=1))
        ) as client:
            client.put("/clients/mm/webhook", json={"url": "https://mm.example/hook"})
            client.post("/orders", json=payload(side="ask", quantity=100, broker_id="mm"))
            client.post("/orders", json=payload(side="bid", quantity=100, broker_id="taker"))
            self.assertEqual(len(first.wait_for(1)), 1)

        # Fresh process over the same log: replay must rebuild state silently.
        second = Recorder()
        with TestClient(
            create_app(db, dispatcher=WebhookDispatcher(sender=second, max_attempts=1))
        ) as client:
            self.assertEqual(client.get("/clients/mm/executions").json()[0]["quantity"], 100)
            self.assertEqual(second.wait_for(1, timeout=0.3), [])


@contextlib.contextmanager
def _closing(harness: EngineHarness):
    try:
        yield harness
    finally:
        harness.close()


@contextlib.contextmanager
def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # let the log create it fresh; keep the unique name
    try:
        yield path
    finally:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path + suffix)


if __name__ == "__main__":
    unittest.main()
