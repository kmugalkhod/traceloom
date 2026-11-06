"""Worker side of webhooks: sign, send, retry.

A queue message `{"type": "webhook_delivery", "delivery_id"}` runs `handle` on a small, bounded
pool of delivery threads (never on the event loop, never on the thread that processes ingestion,
so a slow endpoint cannot hold up traces). The delivery row is the source of truth:

- `handle` first claims the row with one conditional UPDATE that pushes `next_attempt_at`
  forward by a lease (`claim`), so of two workers holding the same message only one sends, and a
  crash makes it due again afterwards;
- 2xx -> succeeded; 5xx, 408, 429, timeouts and network errors -> retried with exponential
  backoff and jitter until `MAX_ATTEMPTS`, then failed; other 4xx and blocked addresses -> failed;
- a disabled webhook fails its pending deliveries, except `webhook.test`, which an admin can send
  to a disabled webhook to check it before turning it on;
- the sweeper (`due_messages`, at most `DUE_BATCH` per pass) re-enqueues anything due, which is
  also how retries fire, how deliveries survive a restart, and how a message dropped by a full
  delivery pool is picked up again.

The request goes to the IP the SSRF guard pinned, with the original Host header and TLS name.
Neither the secret nor the signature is ever logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import random
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from traceloom.db.base import utcnow
from traceloom.db.models import Webhook, WebhookDelivery
from traceloom.llm.crypto import CryptoError
from traceloom.llm.ssrf import BlockedAddress, resolve_target, system_resolver
from traceloom.queue_registry import WEBHOOK_DELIVERY
from traceloom.webhooks.service import TEST_EVENT, WebhookPolicy

log = logging.getLogger("traceloom.webhooks")

MAX_ATTEMPTS = 5
TIMEOUT_SECONDS = 10.0
TIMEOUT = httpx.Timeout(TIMEOUT_SECONDS, connect=3.0)
BACKOFF_BASE_SECONDS = 5.0  # attempt n waits about base * 2**(n-1), plus up to 25% jitter
LEASE_SECONDS = TIMEOUT_SECONDS + 30
DUE_BATCH = 20  # deliveries handed out per sweep or kick
RETRY_STATUSES = frozenset({408, 429})  # plus every 5xx
POOL_WORKERS = 2  # delivery threads, separate from ingestion
POOL_BACKLOG = 100  # queued deliveries beyond this are dropped; the sweeper re-sends them
SIGNATURE_HEADER = "X-Traceloom-Signature"
DELIVERY_HEADER = "X-Traceloom-Delivery"
EVENT_HEADER = "X-Traceloom-Event"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """`t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>`."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def backoff(attempts: int, rng: random.Random | None = None) -> timedelta:
    base = BACKOFF_BASE_SECONDS * 2 ** (attempts - 1)
    return timedelta(seconds=base * (1 + (rng or random).random() * 0.25))


def message(delivery_id: str) -> dict[str, str]:
    return {"type": WEBHOOK_DELIVERY, "delivery_id": delivery_id}


def due_messages(
    session_factory: sessionmaker[Session], now: datetime | None = None
) -> list[dict[str, str]]:
    """Queue messages for pending deliveries that are due (used by the sweeper)."""
    now = now or utcnow()
    with session_factory() as db:
        ids = db.scalars(
            select(WebhookDelivery.id)
            .where(WebhookDelivery.status == "pending", WebhookDelivery.next_attempt_at <= now)
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(DUE_BATCH)
        ).all()
    return [message(i) for i in ids]


def claim(db: Session, delivery_id: str, now: datetime) -> bool:
    """Take the lease on a due, pending delivery. Exactly one concurrent caller gets True."""
    result = db.execute(
        update(WebhookDelivery)
        .where(
            WebhookDelivery.id == delivery_id,
            WebhookDelivery.status == "pending",
            or_(WebhookDelivery.next_attempt_at.is_(None), WebhookDelivery.next_attempt_at <= now),
        )
        .values(next_attempt_at=now + timedelta(seconds=LEASE_SECONDS))
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def kick(container: Any) -> int:
    """Enqueue every due delivery now (call after committing a change that created some)."""
    messages = due_messages(container.session_factory)
    for m in messages:
        container.queue.enqueue(m)
    return len(messages)


def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "[redacted]") if secret else text


def handle(
    container: Any,
    msg: Any,
    *,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Make one delivery attempt. Safe to call twice for the same message."""
    transport = transport or getattr(container, "webhook_transport", None)
    policy = WebhookPolicy.from_settings(
        container.settings, getattr(container, "webhook_resolver", None) or system_resolver
    )
    now = now or utcnow()
    with container.session_factory() as db:
        if not claim(db, msg["delivery_id"], now):
            db.rollback()
            return  # gone, finished, claimed by another worker, or not due yet
        db.commit()
        delivery = db.get(WebhookDelivery, msg["delivery_id"])
        if delivery is None:
            return
        hook = db.get(Webhook, delivery.webhook_id)
        if hook is None or (not hook.enabled and delivery.event != TEST_EVENT):
            _finish(delivery, "failed", now, error="webhook disabled or deleted")
            db.commit()
            return
        try:
            secret = policy.box.decrypt(hook.secret_ciphertext)
        except CryptoError:
            _finish(delivery, "failed", now, error="secret cannot be decrypted")
            db.commit()
            return
        status_code, error, retry = _send(policy, hook.url, delivery, secret, now, transport)
        delivery.attempts += 1
        delivery.last_status_code = status_code
        log.info(
            "webhook delivery %s attempt %d -> %s",
            delivery.id,
            delivery.attempts,
            status_code if status_code is not None else "error",
        )
        if error is None:
            _finish(delivery, "succeeded", now)
        elif retry and delivery.attempts < MAX_ATTEMPTS:
            delivery.last_error = _scrub(error, secret)
            delivery.next_attempt_at = now + backoff(delivery.attempts)
        else:
            _finish(delivery, "failed", now, error=_scrub(error, secret))
        db.commit()


def _finish(delivery: WebhookDelivery, status: str, now: datetime, error: str | None = None):
    delivery.status = status
    delivery.next_attempt_at = None
    delivery.last_error = error
    if status == "succeeded":
        delivery.delivered_at = now


def _send(
    policy: WebhookPolicy,
    url: str,
    delivery: WebhookDelivery,
    secret: str,
    now: datetime,
    transport: httpx.BaseTransport | None,
) -> tuple[int | None, str | None, bool]:
    """(status code, error or None on success, retryable)."""
    try:
        target = resolve_target(url, allow_private=policy.allow_private, resolver=policy.resolver)
    except BlockedAddress as exc:
        return None, f"blocked: {exc}", False
    body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode()
    headers = {
        "Host": target.host_header,
        "Content-Type": "application/json",
        "User-Agent": "Traceloom-Webhooks/1",
        SIGNATURE_HEADER: sign(secret, int(now.timestamp()), body),
        DELIVERY_HEADER: delivery.id,
        EVENT_HEADER: delivery.event,
    }
    try:
        with (
            httpx.Client(timeout=TIMEOUT, transport=transport, trust_env=False) as client,
            client.stream(  # the status is all we need: the reply body is never read
                "POST",
                target.connect_url,
                content=body,
                headers=headers,
                extensions={"sni_hostname": target.host},
                follow_redirects=False,
            ) as response,
        ):
            code = response.status_code
    except httpx.TimeoutException:
        return None, "timeout", True
    except httpx.HTTPError as exc:
        return None, f"network error: {type(exc).__name__}", True
    if 200 <= code < 300:
        return code, None, False
    return code, f"HTTP {code}", code >= 500 or code in RETRY_STATUSES


class DeliveryPool:
    """A few daemon threads that run deliveries off the queue consumer's threads.

    `submit` never blocks: when `backlog` deliveries are already waiting, the message is dropped
    (logged) and the sweeper sends it later, since its row is still pending and due. A delivery id
    that is already waiting is skipped, so a sweep and a kick never queue it twice. Used in full
    mode; the lite in-process queue gives webhook messages a lane of its own instead.
    """

    def __init__(
        self,
        handler: Callable[[Mapping[str, Any]], None],
        workers: int = POOL_WORKERS,
        backlog: int = POOL_BACKLOG,
    ) -> None:
        self._handler = handler
        self._queue: queue.Queue[Mapping[str, Any] | None] = queue.Queue(maxsize=backlog)
        self._waiting: set[Any] = set()  # delivery ids queued and not yet picked up
        self._lock = threading.Lock()
        self._threads = [
            threading.Thread(target=self._run, name=f"traceloom-webhooks-{i}", daemon=True)
            for i in range(workers)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, message: Mapping[str, Any]) -> None:
        delivery_id = message.get("delivery_id")
        with self._lock:
            if delivery_id is not None and delivery_id in self._waiting:
                return
            try:
                self._queue.put_nowait(dict(message))
            except queue.Full:
                log.warning("webhook delivery pool is full; the sweeper will retry")
                return
            if delivery_id is not None:
                self._waiting.add(delivery_id)

    def join(self) -> None:
        """Wait until every submitted delivery has been handled."""
        self._queue.join()

    def stop(self, timeout: float = 10.0) -> None:
        for _ in self._threads:
            try:
                self._queue.put(None, timeout=timeout)
            except queue.Full:
                break  # the threads are daemons; they die with the process
        for thread in self._threads:
            thread.join(timeout)

    def _run(self) -> None:
        while True:
            message = self._queue.get()
            try:
                if message is None:
                    return
                with self._lock:
                    self._waiting.discard(message.get("delivery_id"))
                self._handler(message)
            except Exception:
                log.exception("webhook delivery failed")
            finally:
                self._queue.task_done()
