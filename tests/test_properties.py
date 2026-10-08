"""Randomized invariant checks: seeded streams of random orders and cancels,
with the engine's invariants asserted after every event. Seeds are fixed, so
a failure always reproduces."""
import random
import unittest

from exchange.domain import Side, cents_to_str

from tests.harness import EngineHarness

SYMBOLS = ("AAPL", "PETR4")
EVENTS_PER_STREAM = 400


class RandomStreamInvariantTests(unittest.TestCase):
    def run_stream(self, seed: int) -> None:
        rng = random.Random(seed)
        ex = EngineHarness()
        self.addCleanup(ex.close)
        submitted = []
        for _ in range(EVENTS_PER_STREAM):
            if submitted and rng.random() < 0.1:
                ex.cancel(rng.choice(submitted))
            else:
                order = ex.submit(
                    rng.choice((Side.BID, Side.ASK)),
                    cents_to_str(rng.randint(900, 1100)),
                    rng.randint(1, 500),
                    symbol=rng.choice(SYMBOLS),
                    # Short validities make some orders expire mid-stream,
                    # since the harness clock advances 1s per event.
                    valid_for=rng.choice((5.0, 60.0, 3600.0)),
                )
                submitted.append(order)
            self.check_invariants(ex)

    def check_invariants(self, ex: EngineHarness) -> None:
        engine, now = ex.engine, ex.clock

        # Conservation: fills plus remainder always equal the submitted quantity.
        for order in engine.orders.values():
            traded = sum(t.quantity for t in engine.order_trades(order.id_))
            self.assertEqual(traded + order.remaining, order.quantity)
            self.assertGreaterEqual(order.remaining, 0)

        # Every trade respects both limits and the seller-price rule.
        for trade in engine.trades:
            buy = engine.orders[trade.buy_order_id]
            sell = engine.orders[trade.sell_order_id]
            self.assertIs(buy.side, Side.BID)
            self.assertIs(sell.side, Side.ASK)
            self.assertEqual(trade.symbol, buy.symbol)
            self.assertEqual(trade.symbol, sell.symbol)
            self.assertEqual(trade.price_cents, sell.price_cents)
            self.assertLessEqual(trade.price_cents, buy.price_cents)

        # The book is never left crossed: best live bid below best live ask.
        for book in engine.books.values():
            best_bid = book.bids.peek(now)
            best_ask = book.asks.peek(now)
            if best_bid and best_ask:
                self.assertLess(best_bid.price_cents, best_ask.price_cents)

    def test_seed_0(self) -> None:
        self.run_stream(0)

    def test_seed_1(self) -> None:
        self.run_stream(1)

    def test_seed_2(self) -> None:
        self.run_stream(2)


if __name__ == "__main__":
    unittest.main()
