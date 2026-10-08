"""Deterministic in-process harness that drives the engine through the event
log exactly as the API layer does, but with a controllable clock. The clock
advances one second per event, so chronological priority is implicit in call
order."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import count

from exchange.domain import Order, Side, to_cents
from exchange.engine import Exchange
from exchange.eventlog import EventLog, EventType

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class EngineHarness:
    def __init__(self, log: EventLog | None = None) -> None:
        self.log = log or EventLog(":memory:")
        self.engine = Exchange()
        self.clock = T0
        self._ids = count(1)

    def close(self) -> None:
        self.log.close()

    def tick(self, seconds: float = 1.0) -> None:
        self.clock += timedelta(seconds=seconds)

    def submit(
        self,
        side: Side,
        price: str,
        quantity: int,
        *,
        symbol: str = "AAPL",
        broker_id: str = "broker-1",
        document: str = "111.111.111-11",
        valid_for: float = 3600.0,
        client_order_id: str | None = None,
    ) -> Order:
        order_id = f"O{next(self._ids)}"
        event = self.log.append(
            EventType.ORDER_SUBMITTED,
            {
                "order_id": order_id,
                "broker_id": broker_id,
                "document": document,
                "side": side.value,
                "symbol": symbol,
                "price_cents": to_cents(Decimal(price)),
                "quantity": quantity,
                "valid_until": (self.clock + timedelta(seconds=valid_for)).isoformat(),
                "submitted_at": self.clock.isoformat(),
                "client_order_id": client_order_id,
            },
        )
        self.engine.apply(event)
        self.tick()
        return self.engine.orders[order_id]

    def buy(self, price: str, quantity: int, **kwargs) -> Order:
        return self.submit(Side.BID, price, quantity, **kwargs)

    def sell(self, price: str, quantity: int, **kwargs) -> Order:
        return self.submit(Side.ASK, price, quantity, **kwargs)

    def cancel(self, order: Order) -> None:
        event = self.log.append(
            EventType.ORDER_CANCELLED,
            {"order_id": order.id_, "cancelled_at": self.clock.isoformat()},
        )
        self.engine.apply(event)
        self.tick()
