"""Worker side of webhooks: sign, send, retry.

A queue message `{"type": "webhook_delivery", "delivery_id"}` runs `handle` on a queue thread (never
on the event loop). The delivery row is the source of truth:

- `handle` first claims the row by pushing `next_attempt_at` forward by a lease, so a duplicate
  message or a sweeper pass during the send skips it, and a crash makes it due again afterwards;
- 2xx -> succeeded; 5xx, timeouts and network errors -> retried with exponential backoff and
  jitter until `MAX_ATTEMPTS`, then failed; other 4xx and blocked addresses -> failed;
- the sweeper (`due_messages`) re-enqueues anything due, which is also how retries fire and how
  deliveries survive a restart.

The request goes to the IP the SSRF guard pinned, with the original Host header and TLS name.
Neither the secret nor the signature is ever logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from traceloom.db.base import utcnow
from traceloom.db.models import Webhook, WebhookDelivery
from traceloom.llm.crypto import CryptoError
from traceloom.llm.ssrf import BlockedAddress, resolve_target, system_resolver
from traceloom.queue_registry import WEBHOOK_DELIVERY
from traceloom.webhooks.service import WebhookPolicy

log = logging.getLogger("traceloom.webhooks")

MAX_ATTEMPTS = 5
TIMEOUT_SECONDS = 10.0
BACKOFF_BASE_SECONDS = 5.0  # attempt n waits about base * 2**(n-1), plus up to 25% jitter
LEASE_SECONDS = TIMEOUT_SECONDS + 30
DUE_BATCH = 100
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
        delivery = db.get(WebhookDelivery, msg["delivery_id"])
        if delivery is None or delivery.status != "pending":
            return
        if delivery.next_attempt_at is not None and delivery.next_attempt_at > now:
            return  # claimed by another worker, or not due yet
        hook = db.get(Webhook, delivery.webhook_id)
        if hook is None or not hook.enabled:
            _finish(delivery, "failed", now, error="webhook disabled or deleted")
            db.commit()
            return
        delivery.next_attempt_at = now + timedelta(seconds=LEASE_SECONDS)
        db.commit()
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
        with httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            response = client.post(
                target.connect_url,
                content=body,
                headers=headers,
                extensions={"sni_hostname": target.host},
                follow_redirects=False,
            )
    except httpx.TimeoutException:
        return None, "timeout", True
    except httpx.HTTPError as exc:
        return None, f"network error: {type(exc).__name__}", True
    code = response.status_code
    if 200 <= code < 300:
        return code, None, False
    return code, f"HTTP {code}", code >= 500
