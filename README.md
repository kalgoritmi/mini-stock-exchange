# Mini Exchange

A mini stock exchange, in the spirit of B3 or NYSE at 1:1,000,000 scale: brokers
submit bid (buy) and ask (sell) limit orders over a REST API, and a matching
engine executes trades with price-time priority as soon as prices cross.

Built with Python 3.14, FastAPI, and SQLite. No other infrastructure.

## Quickstart

```sh
docker compose up --build
```

The API is then at `http://localhost:8000` and interactive OpenAPI docs at
`http://localhost:8000/docs`. A worked session:

```sh
# A wants to sell 1000 AAPL at $10
curl -s localhost:8000/orders -H 'content-type: application/json' -d '{
  "broker_id": "broker-a", "document": "111.111.111-11",
  "side": "ask", "symbol": "AAPL", "price": "10.00", "quantity": 1000,
  "valid_until": "2027-01-01T00:00:00Z"
}'

# B wants to buy 1000 AAPL at $20 -> both fill at the seller's $10
curl -s localhost:8000/orders -H 'content-type: application/json' -d '{
  "broker_id": "broker-b", "document": "222.222.222-22",
  "side": "bid", "symbol": "AAPL", "price": "20.00", "quantity": 1000,
  "valid_until": "2027-01-01T00:00:00Z"
}'

curl -s localhost:8000/orders/<id>              # order status by id
curl -s localhost:8000/symbols/AAPL/book        # aggregated depth
curl -s -X DELETE 'localhost:8000/orders/<id>?broker_id=broker-a'   # cancel
```

State survives restarts: the event log lives on the `exchange-data` volume and
the book is rebuilt from it on startup.

### Local development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14.

```sh
uv sync                                          # venv + dependencies
uv run python -m unittest                        # test suite
uv run uvicorn exchange.api:app --reload         # dev server
```

## API

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/orders` | Submit a bid/ask order. Returns the order with its id; may already be (partially) filled if it crossed on arrival. `201`, or `200` on an idempotent replay. |
| `GET` | `/orders/{id}?broker_id=` | Order status, including its executed trades. The owner-only fields (`broker_id`, `document`, `client_order_id`) are returned only when `broker_id` matches the order's owner; anyone else sees the execution facts with those fields `null`. |
| `DELETE` | `/orders/{id}?broker_id=` | Cancel the unfilled remainder of an order. `403` for the wrong broker, `409` if the order is already terminal. |
| `GET` | `/symbols/{symbol}/book` | Aggregated live depth, best price first on both sides. |
| `GET` | `/healthz` | Liveness probe. |

An order submission looks like:

```json
{
  "broker_id": "broker-a",
  "document": "111.111.111-11",
  "side": "ask",
  "symbol": "AAPL",
  "price": "10.00",
  "quantity": 1000,
  "valid_until": "2027-01-01T00:00:00Z",
  "client_order_id": "optional-broker-side-id"
}
```

Notes on the contract:

- **Prices are strings** with at most two decimal places, parsed as exact
  decimals and handled as integer cents internally. Floats never touch money.
- **`valid_until`** must be timezone-aware and in the future. An order is valid
  while `now < valid_until` and its status reads `expired` afterwards.
- **`client_order_id`** is optional idempotency: resubmitting the same order
  with the same `(broker_id, client_order_id)` returns the existing order
  (`200`) instead of double-booking it; reusing the id for a *different*
  payload is a `409`.
- **The document (tax ID) is PII** and is only ever returned to its owning
  broker. A counterparty who learned an order id from a trade sees `document`,
  `broker_id`, and `client_order_id` as `null` — counterparty anonymity plus
  CPF minimization by default. Without authentication the owner check trusts
  the claimed `broker_id`, so this is data minimization, not access control.
- Order statuses: `open`, `partially_filled`, `filled`, `cancelled`, `expired`.
- Validation failures are `422` with field-level detail.

## Matching rules

- A bid's price is the **maximum** the buyer pays; an ask's price is the
  **minimum** the seller accepts. Orders match while the best bid ≥ best ask.
- On a price gap, execution happens at the **seller's price**, per the case
  spec. (Real venues execute at the *resting* order's price — for an incoming
  bid these coincide; for an incoming ask crossing a higher resting bid, the
  spec's rule gives the incoming ask's price and that is what's implemented.)
- Orders can partially execute; the remainder rests on the book at its
  original priority.
- Ties resolve by **price first, then arrival order** (FIFO within a price
  level) — chronological priority exactly as the spec's example requires.

## Design

### Single writer, no locks

Matching requires strict chronological ordering, so the design makes ordering
structural instead of managing it with locks: one process, one event loop, and
the whole validate → append-to-log → match path contains no awaits, so every
order submission is atomic with respect to every other request. Sequential
consistency via a single writer is how real matching engines work (the LMAX
Disruptor is the canonical reference); the production upgrade path is one
writer *per symbol* — the books are fully independent — then partitioning
symbols across processes.

The topology is enforced, not assumed: the event-log connection holds
SQLite's exclusive file lock for its lifetime, so a second engine process on
the same log fails at startup — no locks in the data path, one lock on the
topology. `/healthz` reports the log's persistent `instance_id`, which makes
a misconfigured second exchange on a *different* log detectable from outside.

### Event sourcing

The SQLite **event log is the source of truth**; the order book and all order
state are a deterministic fold over it. On startup the engine replays the log
and arrives at the exact same state, including trade ids and execution
timestamps, because *time is an input*: every event carries its timestamps and
the engine never reads a clock. A trade happens at the instant the taker
order arrives, so the taker's submission time timestamps the trade, and expiry
during matching is evaluated against event time — replay cannot disagree with
the original run. This is also the regulatory reality of exchanges: an
append-only audit trail from which state is derived, not the other way
around. Production path: periodic snapshots so recovery doesn't replay from
genesis, and a real WAL-shipping database.

### Money

Prices cross the API as decimal strings, are validated to two decimal places,
and become integer cents at the boundary. All arithmetic is integer
arithmetic.

### Lazy expiry

Expired orders are skipped (and marked) when they surface at the top of the
book during matching, and every read overlays the validity check, so
observable behaviour is always correct. An eager background sweeper would keep
the book physically smaller but introduces a second writer — deliberately
avoided here; in the per-symbol-worker design it belongs on each symbol's own
writer loop.

### Deliberately out of scope

Auth (brokers are trusted identifiers: the owner checks on reads and cancels
trust the claimed `broker_id`), self-trade prevention, market/stop order
types, rate limiting, and multi-node operation.

## Tests

```sh
uv run python -m unittest -v                             # fast suite, no Docker needed
INTEGRATION=1 uv run python -m unittest tests.test_integration -v   # against the real container
```

- `tests/test_spec_examples.py` — the case statement's worked examples, verbatim.
- `tests/test_matching.py` — price/time priority, FIFO, multi-level sweeps,
  seller-price rule in both directions, symbol isolation.
- `tests/test_expiry.py`, `tests/test_cancel.py` — lifecycle edges.
- `tests/test_replay.py` — crash recovery: state rebuilt identically from the log.
- `tests/test_api.py` — HTTP contract: validation, idempotency, cancel
  semantics, book depth.
- `tests/test_properties.py` — seeded random order streams with invariants
  checked after every event: quantity conservation, both limits respected on
  every trade, and the book never left crossed.
- `tests/test_integration.py` — opt-in (`INTEGRATION=1`, needs Docker):
  builds and starts the compose stack on an isolated project and port,
  exercises the API over the wire, and restarts the container to prove state
  is rebuilt identically from the event log.
