"""The matching engine: deterministic state folded from the event log.

Time is an input — every event carries its own timestamps and the engine
never reads the clock — so replaying the log rebuilds identical state,
including trade ids and execution timestamps.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .book import OrderBook
from .domain import RESTING, Order, OrderStatus, Side, Trade
from .eventlog import Event, EventType


class Exchange:
    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self.trades: list[Trade] = []
        self.trades_by_order: dict[str, list[Trade]] = {}
        self.trades_by_broker: dict[str, list[Trade]] = {}
        self.books: dict[str, OrderBook] = {}
        self._by_client_id: dict[tuple[str, str], str] = {}
        self._order_seq = 0
        self._trade_seq = 0

    def replay(self, events: Iterable[Event]) -> None:
        for event in events:
            self.apply(event)

    def apply(self, event: Event) -> None:
        if event.type_ is EventType.ORDER_SUBMITTED:
            self._apply_submit(event.data)
        elif event.type_ is EventType.ORDER_CANCELLED:
            self._apply_cancel(event.data)
        else:
            raise ValueError(f"unhandled event type: {event.type_}")

    def get_order(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)

    def find_by_client_id(self, broker_id: str, client_order_id: str) -> Order | None:
        order_id = self._by_client_id.get((broker_id, client_order_id))
        return self.orders.get(order_id) if order_id else None

    def order_trades(self, order_id: str) -> list[Trade]:
        return self.trades_by_order.get(order_id, [])

    def client_trades(self, broker_id: str) -> list[Trade]:
        """Every trade a broker took part in, oldest first — the fills behind
        its partial-execution status. A trade appears once even when the broker
        holds both sides; callers expand it per owned order (see the executions
        endpoint)."""
        return self.trades_by_broker.get(broker_id, [])

    def book(self, symbol: str) -> OrderBook:
        return self.books.get(symbol) or OrderBook(symbol)

    def _apply_submit(self, data: dict) -> None:
        self._order_seq += 1
        order = Order(
            id_=data["order_id"],
            seq=self._order_seq,
            broker_id=data["broker_id"],
            document=data["document"],
            side=Side(data["side"]),
            symbol=data["symbol"],
            price_cents=data["price_cents"],
            quantity=data["quantity"],
            remaining=data["quantity"],
            valid_until=datetime.fromisoformat(data["valid_until"]),
            submitted_at=datetime.fromisoformat(data["submitted_at"]),
            client_order_id=data.get("client_order_id"),
        )
        self.orders[order.id_] = order
        if order.client_order_id:
            self._by_client_id[(order.broker_id, order.client_order_id)] = order.id_
        book = self.books.setdefault(order.symbol, OrderBook(order.symbol))
        self._match(book, order)
        if order.remaining > 0:
            book.side_for(order.side).add(order)

    def _apply_cancel(self, data: dict) -> None:
        order = self.orders[data["order_id"]]
        if order.status in RESTING:  # replay-safe: no-op on anything terminal
            order.status = OrderStatus.CANCELLED

    def _match(self, book: OrderBook, taker: Order) -> None:
        # A trade happens at the instant the taker arrives, so the taker's
        # submission time is "now" for expiry checks and trade timestamps.
        now = taker.submitted_at
        makers = book.opposite(taker.side)
        while taker.remaining > 0:
            maker = makers.peek(now)
            if maker is None:
                break
            if taker.side is Side.BID:
                if maker.price_cents > taker.price_cents:
                    break
                buy, sell = taker, maker
            else:
                if maker.price_cents < taker.price_cents:
                    break
                buy, sell = maker, taker
            quantity = min(taker.remaining, maker.remaining)
            for order in (taker, maker):
                order.remaining -= quantity
                order.status = (
                    OrderStatus.FILLED if order.remaining == 0 else OrderStatus.PARTIALLY_FILLED
                )
            self._trade_seq += 1
            trade = Trade(
                id_=str(self._trade_seq),
                symbol=taker.symbol,
                price_cents=sell.price_cents,  # spec: execution happens at the seller's price
                quantity=quantity,
                buy_order_id=buy.id_,
                sell_order_id=sell.id_,
                executed_at=now,
            )
            self.trades.append(trade)
            for order_id in (buy.id_, sell.id_):
                self.trades_by_order.setdefault(order_id, []).append(trade)
            for broker_id in {buy.broker_id, sell.broker_id}:  # set dedupes self-trades
                self.trades_by_broker.setdefault(broker_id, []).append(trade)
