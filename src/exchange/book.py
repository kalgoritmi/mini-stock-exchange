"""Price time priority order book for a single symbol.

Each side keeps an ascending price list mapping to a FIFO
deque per price level. Dead orders (filled, cancelled, expired) are removed
lazily when they surface at the front of the best level.
"""
from __future__ import annotations

from bisect import insort
from collections import defaultdict, deque
from datetime import datetime

from .domain import RESTING, Order, Side, effective_status


class BookSide:
    def __init__(self, side: Side) -> None:
        self.side = side
        self._prices: list[int] = []  # ascending; best is last for bids, first for asks
        self._levels: defaultdict[int, deque[Order]] = defaultdict(deque)

    def add(self, order: Order) -> None:
        if order.price_cents not in self._levels:  # use the dict's O(1) membership as predicate to avoid O(n) search in the list
            insort(self._prices, order.price_cents)  # binary insertion to maintain ascending order
        self._levels[order.price_cents].append(order)  # append to fifo queue for that price level

    def peek(self, now: datetime) -> Order | None:
        """Best live order, discarding dead ones met on the way. Resting
        orders found past their validity flip to EXPIRED here, this is the
        lazy expiry mechanism."""
        best_index = -1 if self.side is Side.BID else 0
        while self._prices:
            price = self._prices[best_index]
            level = self._levels[price]
            while level:
                order = level[0]

                order.status = effective_status(order, now)
                if order.status not in RESTING:
                    level.popleft()
                    continue

                return order
            del self._levels[price]
            del self._prices[best_index]

    def depth(self, now: datetime) -> list[tuple[int, int]]:
        """(price, live quantity) pairs, best price first, read only."""
        prices = reversed(self._prices) if self.side is Side.BID else iter(self._prices)
        out: list[tuple[int, int]] = []
        for price in prices:
            quantity = sum(
                order.remaining
                for order in self._levels[price]
                if order.status in RESTING and not order.is_expired(now)
            )
            if quantity:
                out.append((price, quantity))
        return out


class OrderBook:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.bids = BookSide(Side.BID)
        self.asks = BookSide(Side.ASK)

    def side_for(self, side: Side) -> BookSide:
        return self.bids if side is Side.BID else self.asks

    def opposite(self, side: Side) -> BookSide:
        return self.asks if side is Side.BID else self.bids
