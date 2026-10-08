"""Integration tests against the real container.

These build and start the compose stack on an isolated project name and port,
exercise the HTTP API over the wire, and verify that state survives a
container restart. They need Docker and a few minutes on a cold image build,
so they are opt-in:

    INTEGRATION=1 uv run python -m unittest tests.test_integration -v

Each test uses its own symbol, so tests stay independent while sharing one
running stack.
"""
import os
import subprocess
import time
import unittest
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

import httpx2

PROJECT = "mini-exchange-integration-tests"
PORT = "8123"
BASE_URL = f"http://localhost:{PORT}"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["docker", "compose", "-p", PROJECT, *args],
        cwd=REPO_ROOT,
        env={**os.environ, "EXCHANGE_PORT": PORT},
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{result.stderr}")
    return result


def wait_for_health(deadline_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            if httpx2.get(f"{BASE_URL}/healthz", timeout=2.0).status_code == HTTPStatus.OK:
                return
        except httpx2.HTTPError:
            pass
        time.sleep(1.0)
    raise RuntimeError("exchange container did not become healthy in time")


def setUpModule() -> None:
    if not os.environ.get("INTEGRATION"):
        raise unittest.SkipTest("integration tests are opt-in: set INTEGRATION=1")
    try:
        compose("down", "-v", check=False)  # clean slate even after a crashed run
        compose("up", "-d", "--build")
    except FileNotFoundError:
        raise unittest.SkipTest("docker is not installed")
    wait_for_health()


def tearDownModule() -> None:
    if os.environ.get("INTEGRATION"):
        compose("down", "-v", check=False)


def order_payload(**overrides) -> dict:
    base = {
        "broker_id": "broker-a",
        "document": "111.111.111-11",
        "side": "ask",
        "symbol": "ITEST",
        "price": "10.00",
        "quantity": 1000,
        "valid_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    base.update(overrides)
    return base


class ContainerApiTests(unittest.TestCase):
    def post_order(self, **overrides) -> dict:
        response = httpx2.post(f"{BASE_URL}/orders", json=order_payload(**overrides))
        self.assertEqual(response.status_code, HTTPStatus.CREATED, response.text)
        return response.json()

    def test_health(self) -> None:
        health = httpx2.get(f"{BASE_URL}/healthz").json()
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["instance_id"])

    def test_price_gap_spec_example_over_the_wire(self) -> None:
        ask = self.post_order(side="ask", price="10.00", symbol="GAPX")
        bid = self.post_order(
            side="bid", price="20.00", symbol="GAPX", document="222.222.222-22"
        )
        self.assertEqual(bid["status"], "filled")
        (trade,) = bid["trades"]
        self.assertEqual(trade["price"], "10.00")
        seller = httpx2.get(f"{BASE_URL}/orders/{ask['id']}").json()
        self.assertEqual(seller["status"], "filled")
        self.assertEqual(seller["trades"], [trade])

    def test_book_depth(self) -> None:
        self.post_order(side="bid", price="9.50", quantity=100, symbol="BOOKX")
        self.post_order(side="bid", price="9.80", quantity=200, symbol="BOOKX")
        self.post_order(side="ask", price="10.10", quantity=300, symbol="BOOKX")
        book = httpx2.get(f"{BASE_URL}/symbols/BOOKX/book").json()
        self.assertEqual(
            book["bids"],
            [{"price": "9.80", "quantity": 200}, {"price": "9.50", "quantity": 100}],
        )
        self.assertEqual(book["asks"], [{"price": "10.10", "quantity": 300}])

    def test_validation_over_the_wire(self) -> None:
        response = httpx2.post(f"{BASE_URL}/orders", json=order_payload(price="10.001"))
        self.assertEqual(response.status_code, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_idempotent_resubmission(self) -> None:
        payload = order_payload(symbol="IDEMX", client_order_id="itest-1")
        first = httpx2.post(f"{BASE_URL}/orders", json=payload)
        self.assertEqual(first.status_code, HTTPStatus.CREATED)
        replay = httpx2.post(f"{BASE_URL}/orders", json=payload)
        self.assertEqual(replay.status_code, HTTPStatus.OK)
        self.assertEqual(replay.json()["id"], first.json()["id"])

    def test_cancel_over_the_wire(self) -> None:
        order = self.post_order(symbol="CANX")
        cancelled = httpx2.delete(
            f"{BASE_URL}/orders/{order['id']}", params={"broker_id": "broker-a"}
        )
        self.assertEqual(cancelled.status_code, HTTPStatus.OK)
        self.assertEqual(cancelled.json()["status"], "cancelled")

    def test_state_survives_container_restart(self) -> None:
        maker = self.post_order(side="ask", price="10.00", quantity=500, symbol="DURX")
        self.post_order(
            side="bid", price="10.00", quantity=200, symbol="DURX",
            document="222.222.222-22",
        )
        before = httpx2.get(f"{BASE_URL}/orders/{maker['id']}").json()
        instance_before = httpx2.get(f"{BASE_URL}/healthz").json()["instance_id"]

        compose("restart", "exchange")
        wait_for_health()

        # Replay must rebuild identical state: same fills, trade ids, timestamps.
        after = httpx2.get(f"{BASE_URL}/orders/{maker['id']}").json()
        self.assertEqual(after, before)
        # Identity belongs to the persisted log, not the process.
        instance_after = httpx2.get(f"{BASE_URL}/healthz").json()["instance_id"]
        self.assertEqual(instance_after, instance_before)
        book = httpx2.get(f"{BASE_URL}/symbols/DURX/book").json()
        self.assertEqual(book["asks"], [{"price": "10.00", "quantity": 300}])

        # And the rebuilt book is live, not cosmetic: the remainder still matches.
        second = self.post_order(
            side="bid", price="10.00", quantity=300, symbol="DURX",
            document="333.333.333-33",
        )
        self.assertEqual(second["status"], "filled")


if __name__ == "__main__":
    unittest.main()
