import unittest

from exchange.domain import OrderStatus, effective_status

from tests.harness import EngineHarness


class ExpiryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ex = EngineHarness()
        self.addCleanup(self.ex.close)

    def test_expired_maker_is_skipped_and_marked(self) -> None:
        stale = self.ex.sell("10.00", 100, valid_for=5)
        fresh = self.ex.sell("10.00", 100)
        self.ex.tick(30)  # stale is now past its validity
        buyer = self.ex.buy("10.00", 100)
        self.assertEqual(stale.status, OrderStatus.EXPIRED)
        self.assertEqual(fresh.status, OrderStatus.FILLED)
        self.assertEqual(buyer.status, OrderStatus.FILLED)
        (trade,) = self.ex.engine.trades
        self.assertEqual(trade.sell_order_id, fresh.id_)

    def test_effective_status_reports_expiry_before_book_touches_order(self) -> None:
        order = self.ex.sell("10.00", 100, valid_for=5)
        self.ex.tick(30)
        self.assertEqual(order.status, OrderStatus.OPEN)  # stored state untouched
        self.assertEqual(effective_status(order, self.ex.clock), OrderStatus.EXPIRED)

    def test_partially_filled_order_expires(self) -> None:
        maker = self.ex.sell("10.00", 200, valid_for=5)
        self.ex.buy("10.00", 100)
        self.assertEqual(maker.status, OrderStatus.PARTIALLY_FILLED)
        self.ex.tick(30)
        late_buyer = self.ex.buy("10.00", 100)
        self.assertEqual(maker.status, OrderStatus.EXPIRED)
        self.assertEqual(maker.filled_quantity, 100)  # earlier fill is preserved
        self.assertEqual(late_buyer.status, OrderStatus.OPEN)

    def test_expired_orders_are_excluded_from_depth(self) -> None:
        self.ex.sell("10.00", 100, valid_for=5)
        keeper = self.ex.sell("10.00", 70)
        self.ex.tick(30)
        book = self.ex.engine.book("AAPL")
        self.assertEqual(book.asks.depth(self.ex.clock), [(1000, keeper.remaining)])


if __name__ == "__main__":
    unittest.main()
