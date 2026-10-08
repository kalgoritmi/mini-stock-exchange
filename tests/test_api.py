import unittest
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

from fastapi.testclient import TestClient

from exchange.api import create_app


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


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = self.enterContext(TestClient(create_app(":memory:")))

    def test_health_exposes_instance_identity(self) -> None:
        health = self.client.get("/healthz").json()
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["instance_id"])

    def test_submit_and_fetch_order(self) -> None:
        created = self.client.post("/orders", json=payload())
        self.assertEqual(created.status_code, HTTPStatus.CREATED)
        body = created.json()
        self.assertEqual(body["status"], "open")
        self.assertEqual(body["price"], "10.00")
        self.assertEqual(created.headers["Location"], f"/orders/{body['id']}")

        fetched = self.client.get(f"/orders/{body['id']}", params={"broker_id": "broker-1"})
        self.assertEqual(fetched.status_code, HTTPStatus.OK)
        self.assertEqual(fetched.json(), body)

    def test_owner_only_fields_are_redacted_for_non_owners(self) -> None:
        order = self.client.post("/orders", json=payload(client_order_id="ref-1")).json()
        self.assertEqual(order["document"], "111.111.111-11")  # submitter's own view

        for params in ({}, {"broker_id": "broker-2"}):
            with self.subTest(params=params):
                view = self.client.get(f"/orders/{order['id']}", params=params).json()
                self.assertIsNone(view["document"])
                self.assertIsNone(view["broker_id"])
                self.assertIsNone(view["client_order_id"])
                self.assertEqual(view["status"], "open")  # execution facts stay visible

        owner_view = self.client.get(
            f"/orders/{order['id']}", params={"broker_id": "broker-1"}
        ).json()
        self.assertEqual(owner_view["document"], "111.111.111-11")

    def test_price_gap_executes_at_seller_price_end_to_end(self) -> None:
        ask = self.client.post("/orders", json=payload(side="ask", price="10.00")).json()
        bid = self.client.post(
            "/orders", json=payload(side="bid", price="20.00", document="222.222.222-22")
        ).json()
        self.assertEqual(bid["status"], "filled")
        (trade,) = bid["trades"]
        self.assertEqual(trade["price"], "10.00")
        self.assertEqual(trade["sell_order_id"], ask["id"])
        # The seller sees the same trade.
        seller_view = self.client.get(f"/orders/{ask['id']}").json()
        self.assertEqual(seller_view["status"], "filled")
        self.assertEqual(seller_view["trades"], [trade])

    def test_book_depth_is_aggregated_and_sorted(self) -> None:
        self.client.post("/orders", json=payload(side="bid", price="9.50", quantity=100))
        self.client.post("/orders", json=payload(side="bid", price="9.80", quantity=200))
        self.client.post("/orders", json=payload(side="bid", price="9.80", quantity=50))
        self.client.post("/orders", json=payload(side="ask", price="10.10", quantity=300))

        book = self.client.get("/symbols/AAPL/book").json()
        self.assertEqual(
            book["bids"],
            [{"price": "9.80", "quantity": 250}, {"price": "9.50", "quantity": 100}],
        )
        self.assertEqual(book["asks"], [{"price": "10.10", "quantity": 300}])

    def test_book_for_unknown_symbol_is_empty(self) -> None:
        book = self.client.get("/symbols/ZZZZ/book").json()
        self.assertEqual((book["bids"], book["asks"]), ([], []))

    def test_validation_rejections(self) -> None:
        cases = {
            "price with more than two decimal places": payload(price="10.001"),
            "negative price": payload(price="-5.00"),
            "zero quantity": payload(quantity=0),
            "invalid side": payload(side="hold"),
            "lowercase symbol": payload(symbol="aapl"),
            "naive valid_until": payload(valid_until="2030-01-01T12:00:00"),
            "past valid_until": payload(valid_until="2020-01-01T12:00:00+00:00"),
        }
        for name, bad in cases.items():
            with self.subTest(name):
                response = self.client.post("/orders", json=bad)
                self.assertEqual(response.status_code, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_get_unknown_order_is_404(self) -> None:
        self.assertEqual(self.client.get("/orders/nope").status_code, HTTPStatus.NOT_FOUND)

    def test_idempotent_resubmission_with_client_order_id(self) -> None:
        body = payload(client_order_id="abc-1")
        first = self.client.post("/orders", json=body)
        self.assertEqual(first.status_code, HTTPStatus.CREATED)

        replay = self.client.post("/orders", json=body)
        self.assertEqual(replay.status_code, HTTPStatus.OK)
        self.assertEqual(replay.json()["id"], first.json()["id"])

        conflicting = self.client.post("/orders", json=body | {"quantity": 999})
        self.assertEqual(conflicting.status_code, HTTPStatus.CONFLICT)

    def test_cancel_lifecycle(self) -> None:
        order = self.client.post("/orders", json=payload()).json()

        wrong_broker = self.client.delete(
            f"/orders/{order['id']}", params={"broker_id": "broker-2"}
        )
        self.assertEqual(wrong_broker.status_code, HTTPStatus.FORBIDDEN)

        cancelled = self.client.delete(
            f"/orders/{order['id']}", params={"broker_id": "broker-1"}
        )
        self.assertEqual(cancelled.status_code, HTTPStatus.OK)
        self.assertEqual(cancelled.json()["status"], "cancelled")

        again = self.client.delete(f"/orders/{order['id']}", params={"broker_id": "broker-1"})
        self.assertEqual(again.status_code, HTTPStatus.CONFLICT)

        missing = self.client.delete("/orders/nope", params={"broker_id": "broker-1"})
        self.assertEqual(missing.status_code, HTTPStatus.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
