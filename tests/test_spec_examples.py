"""The worked examples from the case statement, encoded verbatim."""
import unittest

from exchange.domain import OrderStatus

from tests.harness import EngineHarness


class SpecExampleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ex = EngineHarness()
        self.addCleanup(self.ex.close)

    def test_example_1_same_price(self) -> None:
        a = self.ex.sell("10.00", 1000)
        b = self.ex.buy("10.00", 1000)
        self.assertEqual(a.status, OrderStatus.FILLED)
        self.assertEqual(b.status, OrderStatus.FILLED)
        (trade,) = self.ex.engine.trades
        self.assertEqual(trade.price_cents, 1000)
        self.assertEqual(trade.quantity, 1000)

    def test_example_2_no_match(self) -> None:
        a = self.ex.sell("20.00", 1000)
        b = self.ex.buy("10.00", 1000)
        self.assertEqual(a.status, OrderStatus.OPEN)
        self.assertEqual(b.status, OrderStatus.OPEN)
        self.assertEqual(self.ex.engine.trades, [])

    def test_example_3_price_gap_executes_at_seller_price(self) -> None:
        a = self.ex.sell("10.00", 1000)
        b = self.ex.buy("20.00", 1000)
        self.assertEqual(a.status, OrderStatus.FILLED)
        self.assertEqual(b.status, OrderStatus.FILLED)
        (trade,) = self.ex.engine.trades
        self.assertEqual(trade.price_cents, 1000)

    def test_partial_execution(self) -> None:
        a = self.ex.sell("10.00", 1000)
        b = self.ex.buy("10.00", 500)
        self.assertEqual(a.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(a.remaining, 500)
        self.assertEqual(b.status, OrderStatus.FILLED)

    def test_partial_execution_across_multiple_makers(self) -> None:
        a = self.ex.sell("10.00", 500)
        b = self.ex.sell("10.00", 500)
        c = self.ex.buy("10.00", 1500)
        self.assertEqual(a.status, OrderStatus.FILLED)
        self.assertEqual(b.status, OrderStatus.FILLED)
        self.assertEqual(c.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(c.remaining, 500)

    def test_chronological_priority(self) -> None:
        a = self.ex.sell("10.00", 1000)
        b = self.ex.sell("10.00", 1000)
        c = self.ex.buy("10.00", 1000)
        self.assertEqual(a.status, OrderStatus.FILLED)
        self.assertEqual(b.status, OrderStatus.OPEN)
        self.assertEqual(c.status, OrderStatus.FILLED)
        (trade,) = self.ex.engine.trades
        self.assertEqual(trade.sell_order_id, a.id_)


if __name__ == "__main__":
    unittest.main()
