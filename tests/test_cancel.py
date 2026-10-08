import unittest

from exchange.domain import OrderStatus

from tests.harness import EngineHarness


class CancelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ex = EngineHarness()
        self.addCleanup(self.ex.close)

    def test_cancelled_order_no_longer_matches(self) -> None:
        maker = self.ex.sell("10.00", 100)
        self.ex.cancel(maker)
        buyer = self.ex.buy("10.00", 100)
        self.assertEqual(maker.status, OrderStatus.CANCELLED)
        self.assertEqual(buyer.status, OrderStatus.OPEN)
        self.assertEqual(self.ex.engine.trades, [])

    def test_cancel_preserves_earlier_fills(self) -> None:
        maker = self.ex.sell("10.00", 200)
        self.ex.buy("10.00", 100)
        self.ex.cancel(maker)
        self.assertEqual(maker.status, OrderStatus.CANCELLED)
        self.assertEqual(maker.filled_quantity, 100)

    def test_cancel_is_a_noop_on_terminal_orders(self) -> None:
        maker = self.ex.sell("10.00", 100)
        self.ex.buy("10.00", 100)
        self.ex.cancel(maker)  # already filled; the engine must not resurrect it
        self.assertEqual(maker.status, OrderStatus.FILLED)


if __name__ == "__main__":
    unittest.main()
