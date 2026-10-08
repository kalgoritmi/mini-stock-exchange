"""Core domain types. Prices are integer cents end to end; floats never touch money."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    BID = "bid"
    ASK = "ask"


class OrderStatus(StrEnum):
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# statuses in which an order still rests on the book and can trade or be cancelled, not fulfilled.
RESTING = (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, )


@dataclass
class Order:
    id_: str
    seq: int
    broker_id: str
    document: str
    side: Side
    symbol: str
    price_cents: int
    quantity: int
    remaining: int
    valid_until: datetime
    submitted_at: datetime
    client_order_id: str | None = None
    status: OrderStatus = OrderStatus.OPEN

    @property
    def filled_quantity(self) -> int:
        return self.quantity - self.remaining

    def is_expired(self, now: datetime) -> bool:
        """Validity is the half-open interval [submitted_at, valid_until)."""
        return now >= self.valid_until


def effective_status(order: Order, now: datetime) -> OrderStatus:
    """Expiry is lazy: the stored status only flips when the book touches the
    order, so every read must overlay the validity check."""
    if order.status in RESTING and order.is_expired(now):
        return OrderStatus.EXPIRED
    return order.status


@dataclass(frozen=True)
class Trade:
    id_: str
    symbol: str
    price_cents: int
    quantity: int
    buy_order_id: str
    sell_order_id: str
    executed_at: datetime


def to_cents(price: Decimal) -> int:
    """Callers validate scale <= 2 before calling, so the quantize is exact."""
    return int(price.quantize(Decimal("0.01")).scaleb(2))


def cents_to_str(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"
