import unittest

from exchange.domain import OrderStatus

from tests.harness import EngineHarness


class MatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ex = EngineHarness()
        self.addCleanup(self.ex.close)

    def test_incoming_ask_executes_at_its_own_price(self) -> None:
        # The seller-price rule in the other direction: the resting order is
        # the bid, so the incoming ask's price wins.
        self.ex.buy("20.00", 100)
        self.ex.sell("10.00", 100)
        (trade,) = self.ex.engine.trades
        self.assertEqual(trade.price_cents, 1000)

    def test_price_priority_beats_time_priority(self) -> None:
        worse = self.ex.sell("10.50", 100)
        better = self.ex.sell("10.00", 100)  # later, but cheaper
        self.ex.buy("10.50", 100)
        self.assertEqual(better.status, OrderStatus.FILLED)
        self.assertEqual(worse.status, OrderStatus.OPEN)
        (trade,) = self.ex.engine.trades
        self.assertEqual(trade.price_cents, 1000)

    def test_fifo_within_price_level(self) -> None:
        a = self.ex.sell("10.00", 100)
        b = self.ex.sell("10.00", 100)
        self.ex.buy("10.00", 150)
        self.assertEqual(a.status, OrderStatus.FILLED)
        self.assertEqual(b.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(b.remaining, 50)
        first, second = self.ex.engine.trades
        self.assertEqual((first.sell_order_id, first.quantity), (a.id_, 100))
        self.assertEqual((second.sell_order_id, second.quantity), (b.id_, 50))

    def test_sweep_multiple_price_levels(self) -> None:
        self.ex.sell("10.00", 100)
        self.ex.sell("10.50", 100)
        buyer = self.ex.buy("11.00", 250)
        first, second = self.ex.engine.trades
        self.assertEqual((first.price_cents, first.quantity), (1000, 100))
        self.assertEqual((second.price_cents, second.quantity), (1050, 100))
        self.assertEqual(buyer.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(buyer.remaining, 50)
        # The remainder rests as the best (and only) bid.
        book = self.ex.engine.book("AAPL")
        self.assertEqual(book.bids.depth(self.ex.clock), [(1100, 50)])

    def test_resting_remainder_matches_later(self) -> None:
        buyer = self.ex.buy("10.00", 300)
        self.ex.sell("10.00", 100)
        self.assertEqual(buyer.status, OrderStatus.PARTIALLY_FILLED)
        self.ex.sell("10.00", 200)
        self.assertEqual(buyer.status, OrderStatus.FILLED)

    def test_orders_for_different_symbols_do_not_interact(self) -> None:
        a = self.ex.sell("10.00", 100, symbol="AAPL")
        b = self.ex.buy("10.00", 100, symbol="PETR4")
        self.assertEqual(a.status, OrderStatus.OPEN)
        self.assertEqual(b.status, OrderStatus.OPEN)
        self.assertEqual(self.ex.engine.trades, [])


if __name__ == "__main__":
    unittest.main()
