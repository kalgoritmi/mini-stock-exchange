"""Broker-facing REST API.

Consistency model: one process, one event loop, and no awaits inside the
validate -> append -> apply sections below, so every mutation is atomic with
respect to every other request. That single writer discipline is what makes
matching strictly chronological (see README).
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from http import HTTPStatus
from pathlib import Path as FsPath
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from .domain import (
    RESTING,
    Order,
    OrderStatus,
    Side,
    Trade,
    cents_to_str,
    effective_status,
    to_cents,
)
from .engine import Exchange
from .eventlog import EventLog, EventType
from .webhook import WebhookDelivery, WebhookDispatcher

SYMBOL_PATTERN = r"^[A-Z][A-Z0-9]{0,9}$"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SubmitOrderRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    broker_id: str = Field(min_length=1, max_length=64, examples=["broker-1"])
    document: str = Field(min_length=1, max_length=32, examples=["111.111.111-11"])
    side: Side
    symbol: str = Field(pattern=SYMBOL_PATTERN, examples=["AAPL"])
    price: Decimal = Field(gt=0, max_digits=12, decimal_places=2, examples=["10.00"])
    quantity: int = Field(gt=0, le=1_000_000_000)
    valid_until: datetime
    client_order_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("valid_until")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("must include a timezone offset, e.g. 2026-07-07T15:00:00Z")
        return value


class TradeResponse(BaseModel):
    id_: str = Field(serialization_alias="id")
    symbol: str
    price: str
    quantity: int
    buy_order_id: str
    sell_order_id: str
    executed_at: datetime

    @classmethod
    def from_domain(cls, trade: Trade) -> TradeResponse:
        return cls(
            id_=trade.id_,
            symbol=trade.symbol,
            price=cents_to_str(trade.price_cents),
            quantity=trade.quantity,
            buy_order_id=trade.buy_order_id,
            sell_order_id=trade.sell_order_id,
            executed_at=trade.executed_at,
        )


class OrderResponse(BaseModel):
    id_: str = Field(serialization_alias="id")
    # Owner-only fields, null for anyone but the owning broker: the document
    # is a tax id — PII that a counterparty who learned this order's id
    # from a trade must not see — and broker_id/client_order_id would
    # otherwise leak who is behind the order (and the claim to unredact it).
    broker_id: str | None
    document: str | None
    side: Side
    symbol: str
    price: str
    quantity: int
    filled_quantity: int
    remaining_quantity: int
    status: OrderStatus
    valid_until: datetime
    submitted_at: datetime
    client_order_id: str | None
    trades: list[TradeResponse]

    @classmethod
    def from_domain(
        cls, order: Order, trades: list[Trade], now: datetime, *, owner_view: bool
    ) -> OrderResponse:
        return cls(
            id_=order.id_,
            broker_id=order.broker_id if owner_view else None,
            document=order.document if owner_view else None,
            side=order.side,
            symbol=order.symbol,
            price=cents_to_str(order.price_cents),
            quantity=order.quantity,
            filled_quantity=order.filled_quantity,
            remaining_quantity=order.remaining,
            status=effective_status(order, now),
            valid_until=order.valid_until,
            submitted_at=order.submitted_at,
            client_order_id=order.client_order_id if owner_view else None,
            trades=[TradeResponse.from_domain(trade) for trade in trades],
        )


class BookLevelResponse(BaseModel):
    price: str
    quantity: int


class BookResponse(BaseModel):
    symbol: str
    bids: list[BookLevelResponse]
    asks: list[BookLevelResponse]
    as_of: datetime


class ExecutionReport(BaseModel):
    """Cumulative execution status for one order: how much of it has filled so
    far, and at what limit price. Emitted on every fill and served by the
    executions endpoint.

    It is a snapshot, not an event. Re-delivery is harmless because `quantity`
    (the cumulative filled amount) only ever grows, so it doubles as the
    version a subscriber uses to dedupe — see the executions endpoint."""

    order_id: str
    price: str
    quantity: int  # cumulative filled quantity, monotonically increasing

    @classmethod
    def from_order(cls, order: Order) -> ExecutionReport:
        return cls(
            order_id=order.id_,
            price=cents_to_str(order.price_cents),
            quantity=order.filled_quantity,
        )


class WebhookSubscription(BaseModel):
    url: HttpUrl


def _same_submission(order: Order, body: SubmitOrderRequest) -> bool:
    return (
        order.document == body.document
        and order.side is body.side
        and order.symbol == body.symbol
        and order.price_cents == to_cents(body.price)
        and order.quantity == body.quantity
        and order.valid_until == body.valid_until
    )


async def _engine(request: Request) -> Exchange:
    return request.app.state.engine


async def _log(request: Request) -> EventLog:
    return request.app.state.log


async def _dispatcher(request: Request) -> WebhookDispatcher:
    return request.app.state.dispatcher


async def _subscriptions(request: Request) -> dict[str, str]:
    return request.app.state.subscriptions


EngineDep = Annotated[Exchange, Depends(_engine)]
LogDep = Annotated[EventLog, Depends(_log)]
DispatcherDep = Annotated[WebhookDispatcher, Depends(_dispatcher)]
SubscriptionsDep = Annotated[dict[str, str], Depends(_subscriptions)]

router = APIRouter()


def _notify_fills(
    engine: Exchange,
    dispatcher: WebhookDispatcher,
    subscriptions: dict[str, str],
    new_trades: list[Trade],
) -> None:
    """Push one cumulative ExecutionReport per order the fills touched. Runs on
    the event-loop thread (the single writer), so reading engine state and
    building the payloads here is race-free; the worker thread only ever sees
    the immutable deliveries. Intra-batch fills of one order are conflated into
    its final cumulative state — see ExecutionReport."""
    affected: dict[str, Order] = {}
    for trade in new_trades:
        for order_id in (trade.buy_order_id, trade.sell_order_id):
            order = engine.get_order(order_id)
            if order is not None:
                affected[order_id] = order  # last write wins: final cumulative state
    for order in affected.values():
        url = subscriptions.get(order.broker_id)
        if url is None:
            continue
        report = ExecutionReport.from_order(order)
        dispatcher.enqueue(
            WebhookDelivery(
                url=url,
                payload=report.model_dump(by_alias=True, mode="json"),
                idempotency_key=order.id_,
            )
        )


def order_response(
    engine: Exchange, order: Order, now: datetime, *, owner_view: bool
) -> OrderResponse:
    return OrderResponse.from_domain(
        order, engine.order_trades(order.id_), now, owner_view=owner_view
    )


@router.post("/orders", status_code=HTTPStatus.CREATED, response_model=OrderResponse)
async def submit_order(
    body: SubmitOrderRequest,
    engine: EngineDep,
    log: LogDep,
    dispatcher: DispatcherDep,
    subscriptions: SubscriptionsDep,
    response: Response,
) -> OrderResponse:
    now = utcnow()
    if body.valid_until <= now:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="valid_until must be in the future",
        )

    if body.client_order_id:
        existing = engine.find_by_client_id(body.broker_id, body.client_order_id)
        if existing is not None:
            if not _same_submission(existing, body):
                raise HTTPException(
                    status_code=HTTPStatus.CONFLICT,
                    detail=f"client_order_id {body.client_order_id!r} was already "
                    "used for a different order",
                )
            response.status_code = HTTPStatus.OK
            response.headers["Location"] = f"/orders/{existing.id_}"
            return order_response(engine, existing, now, owner_view=True)

    data = {
        "order_id": str(uuid4()),
        "broker_id": body.broker_id,
        "document": body.document,
        "side": body.side.value,
        "symbol": body.symbol,
        "price_cents": to_cents(body.price),
        "quantity": body.quantity,
        "valid_until": body.valid_until.isoformat(),
        "submitted_at": now.isoformat(),
        "client_order_id": body.client_order_id,
    }
    # Snapshot the trade log around apply(); everything appended is a fill from
    # this order. Capturing it here, not inside the engine, is what keeps
    # replay side-effect-free — restarts never re-fire notifications.
    before = len(engine.trades)
    event = log.append(EventType.ORDER_SUBMITTED, data)
    engine.apply(event)
    order = engine.get_order(data["order_id"])
    assert order is not None
    response.headers["Location"] = f"/orders/{order.id_}"
    _notify_fills(engine, dispatcher, subscriptions, engine.trades[before:])
    return order_response(engine, order, now, owner_view=True)


@router.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    engine: EngineDep,
    broker_id: str | None = Query(
        default=None, description="Broker claiming the order; reveals the owner-only fields"
    ),
) -> OrderResponse:
    order = engine.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="order not found")
    # Ownership is a claim, not a credential (auth is out of scope): this is
    # PII minimization for counterparties, not access control.
    return order_response(engine, order, utcnow(), owner_view=broker_id == order.broker_id)


@router.delete("/orders/{order_id}", response_model=OrderResponse)
async def cancel_order(
    order_id: str,
    engine: EngineDep,
    log: LogDep,
    broker_id: str = Query(min_length=1, description="Broker that owns the order"),
) -> OrderResponse:
    order = engine.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="order not found")
    if order.broker_id != broker_id:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN, detail="order belongs to another broker"
        )
    now = utcnow()
    status = effective_status(order, now)
    if status not in RESTING:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=f"order is {status} and can no longer be cancelled",
        )
    event = log.append(
        EventType.ORDER_CANCELLED, {"order_id": order_id, "cancelled_at": now.isoformat()}
    )
    engine.apply(event)
    return order_response(engine, order, now, owner_view=True)


@router.get("/symbols/{symbol}/book", response_model=BookResponse)
async def get_book(
    engine: EngineDep, symbol: str = Path(pattern=SYMBOL_PATTERN)
) -> BookResponse:
    now = utcnow()
    book = engine.book(symbol)
    return BookResponse(
        symbol=symbol,
        bids=[
            BookLevelResponse(price=cents_to_str(price), quantity=quantity)
            for price, quantity in book.bids.depth(now)
        ],
        asks=[
            BookLevelResponse(price=cents_to_str(price), quantity=quantity)
            for price, quantity in book.asks.depth(now)
        ],
        as_of=now,
    )


@router.put(
    "/clients/{broker_id}/webhook",
    status_code=HTTPStatus.NO_CONTENT,
    response_class=Response,
)
async def register_webhook(
    broker_id: str, body: WebhookSubscription, subscriptions: SubscriptionsDep
) -> Response:
    # Idempotent by design: re-registering just overwrites the callback URL.
    # In-memory on purpose — subscriptions are delivery config, not market
    # facts, so they don't belong in the event log (a broker re-registers on
    # reconnect and reconciles via the executions endpoint).
    subscriptions[broker_id] = str(body.url)
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.get("/clients/{broker_id}/executions", response_model=list[ExecutionReport])
async def client_executions(broker_id: str, engine: EngineDep) -> list[ExecutionReport]:
    # The pull counterpart to the webhook and the source of truth for
    # reconciliation: the current cumulative status of every order this broker
    # has fills on, oldest order first.
    orders: dict[str, Order] = {}
    for trade in engine.client_trades(broker_id):
        for order_id in (trade.buy_order_id, trade.sell_order_id):
            order = engine.get_order(order_id)
            if order is not None and order.broker_id == broker_id:
                orders[order_id] = order
    return [ExecutionReport.from_order(order) for order in orders.values()]


@router.get("/healthz")
async def healthz(log: LogDep) -> dict[str, str]:
    # instance_id is the log's identity, not the process's: it lets anything
    # comparing two backends detect a split-brain (two engines on two logs).
    return {"status": "ok", "instance_id": log.instance_id}


def create_app(
    db_path: str | None = None, *, dispatcher: WebhookDispatcher | None = None
) -> FastAPI:
    resolved_db = db_path or os.environ.get("EXCHANGE_DB", "data/exchange.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if resolved_db != ":memory:":
            FsPath(resolved_db).parent.mkdir(parents=True, exist_ok=True)
        log = EventLog(resolved_db)
        engine = Exchange()
        # Side-effect-free: replay calls engine.apply() directly and never the
        # notify path in submit_order, so restarts rebuild state without
        # re-firing a single webhook.
        engine.replay(log.replay())
        webhooks = dispatcher or WebhookDispatcher()
        webhooks.start()
        app.state.log = log
        app.state.engine = engine
        app.state.dispatcher = webhooks
        app.state.subscriptions = {}
        yield
        webhooks.stop()  # drains queued deliveries, then joins the worker
        log.close()

    app = FastAPI(
        title="Mini Exchange",
        version="0.1.0",
        description="A mini stock exchange: brokers submit bid/ask orders, "
        "the engine matches them with price/time priority.",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
