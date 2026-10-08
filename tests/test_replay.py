import os
import tempfile
import unittest

from exchange.engine import Exchange
from exchange.eventlog import EventLog

from tests.harness import EngineHarness


class ReplayTests(unittest.TestCase):
    def test_state_is_rebuilt_identically_from_the_log(self) -> None:
        """Cold start: fold the persisted log into a fresh engine and get the
        same orders, trades, ids, and timestamps — the crash-recovery path."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "exchange.db")

            original = EngineHarness(EventLog(path))
            seller = original.sell("10.00", 1000)
            original.buy("10.00", 600)
            original.buy("12.50", 300, symbol="PETR4")
            original.cancel(seller)  # cancels the partially filled remainder
            original.log.close()

            rebuilt = Exchange()
            log = EventLog(path)
            rebuilt.replay(log.replay())
            log.close()

            self.assertEqual(rebuilt.orders, original.engine.orders)
            self.assertEqual(rebuilt.trades, original.engine.trades)

    def test_replayed_engine_keeps_matching_correctly(self) -> None:
        """The rebuilt book must be live, not just cosmetically equal: a new
        order after recovery matches against the replayed resting orders."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "exchange.db")

            original = EngineHarness(EventLog(path))
            original.sell("10.00", 500)
            original.log.close()

            recovered = EngineHarness(EventLog(path))
            self.addCleanup(recovered.close)
            recovered.engine.replay(recovered.log.replay())
            recovered.tick(60)
            buyer = recovered.buy("10.00", 500)
            self.assertEqual(buyer.filled_quantity, 500)


if __name__ == "__main__":
    unittest.main()
