import os
import sqlite3
import tempfile
import unittest

from exchange.eventlog import EventLog


class SingleWriterLockTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "exchange.db")

    def test_second_writer_is_locked_out(self) -> None:
        """One writer per log, enforced: a second engine process opening the
        same log must fail at startup, not at its first order."""
        first = EventLog(self.path)
        self.addCleanup(first.close)
        with self.assertRaises(sqlite3.OperationalError):
            EventLog(self.path)

    def test_lock_is_released_on_close_and_identity_is_stable(self) -> None:
        first = EventLog(self.path)
        first_id = first.instance_id
        first.close()
        second = EventLog(self.path)  # failover: same log, new process
        self.addCleanup(second.close)
        self.assertEqual(second.instance_id, first_id)

    def test_each_log_has_its_own_identity(self) -> None:
        a = EventLog(self.path)
        self.addCleanup(a.close)
        b = EventLog(os.path.join(os.path.dirname(self.path), "other.db"))
        self.addCleanup(b.close)
        self.assertNotEqual(a.instance_id, b.instance_id)


if __name__ == "__main__":
    unittest.main()
