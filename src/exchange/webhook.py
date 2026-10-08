"""Out-of-band delivery of execution reports to brokers.

Delivery runs on a dedicated worker thread draining a thread-safe queue, so the
API's single-writer event loop never blocks on webhook I/O. Producers (request
handlers on the event loop) only ever call `enqueue`, a non-blocking, thread-safe
put.

Nothing here touches engine state: handlers resolve each notification into an
immutable WebhookDelivery on the event-loop thread before enqueueing, so the
queue is the sole synchronization boundary between the two threads.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Callable

log = logging.getLogger("exchange.webhook")


@dataclass(frozen=True)
class WebhookDelivery:
    url: str
    payload: dict[str, Any]  # already JSON-ready, built on the producer thread
    idempotency_key: str


# A sender turns one delivery into a network call. It raises on failure so the
# dispatcher retries; returning normally means delivered. Injectable so tests
# capture deliveries without a real HTTP endpoint.
Sender = Callable[[WebhookDelivery], None]


def http_post(delivery: WebhookDelivery, *, timeout: float = 5.0) -> None:
    """Default sender: a blocking POST. Blocking is fine — it runs on the
    worker thread, never the event loop. Raises on any non-2xx or transport
    error so the dispatcher retries."""
    request = urllib.request.Request(
        delivery.url,
        data=json.dumps(delivery.payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            # Deliveries are at-least-once; brokers dedupe on this key.
            "X-Idempotency-Key": delivery.idempotency_key,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status >= HTTPStatus.MULTIPLE_CHOICES:  # anything past 2xx
            raise urllib.error.HTTPError(
                delivery.url, response.status, "webhook rejected", response.headers, None
            )


class WebhookDispatcher:
    """A queue drained by one worker thread. Start it in the app lifespan and
    stop it on shutdown; enqueue from request handlers."""

    _SENTINEL = object()

    def __init__(
        self,
        *,
        sender: Sender = http_post,
        max_queue: int = 10_000,
        max_attempts: int = 5,
    ) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=max_queue)
        self._sender = sender
        self._max_attempts = max_attempts
        self._thread = threading.Thread(
            target=self._run, name="webhook-dispatcher", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        # The sentinel goes through the same FIFO queue, so everything already
        # enqueued is delivered before the worker sees it and exits.
        self._q.put(self._SENTINEL)
        self._thread.join(timeout)

    def enqueue(self, delivery: WebhookDelivery) -> None:
        try:
            self._q.put_nowait(delivery)
        except queue.Full:
            # Never block the event loop. Drop and log; the executions endpoint
            # stays the source of truth for reconciliation.
            log.error("webhook queue full, dropped %s", delivery.idempotency_key)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            try:
                if item is self._SENTINEL:
                    return
                self._deliver(item)
            finally:
                self._q.task_done()

    def _deliver(self, delivery: WebhookDelivery) -> None:
        backoff = 0.5
        for attempt in range(1, self._max_attempts + 1):
            try:
                self._sender(delivery)
                return
            except Exception as exc:  # noqa: BLE001 - worker isolates every failure
                log.warning(
                    "webhook attempt %d/%d failed (%s): %s",
                    attempt, self._max_attempts, delivery.idempotency_key, exc,
                )
            if attempt < self._max_attempts:
                time.sleep(backoff)  # sleeps the worker thread, not the loop
                backoff = min(backoff * 2, 8.0)
        log.error("webhook dead-lettered after retries: %s", delivery.idempotency_key)
