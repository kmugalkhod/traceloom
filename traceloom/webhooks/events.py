"""Webhook events (FR-P10): write one pending delivery row per matching enabled webhook.

This only enqueues. Signing, sending and retries belong to the worker. Enqueueing runs in a
savepoint and never raises, so a webhook problem cannot fail the change that caused the event.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceloom.db.base import new_id, utcnow
from traceloom.db.models import Webhook, WebhookDelivery

log = logging.getLogger("traceloom")

LABEL_MOVED = "prompt.label_moved"
VERSION_CREATED = "prompt.version_created"
EVENTS = (LABEL_MOVED, VERSION_CREATED)


def enqueue(
    db: Session,
    project_id: str,
    event: str,
    data: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[WebhookDelivery]:
    """Add a pending delivery for every enabled webhook of the project subscribed to `event`.

    The rows commit with the caller's transaction. Returns the rows added ([] on any failure).
    """
    now = now or utcnow()
    try:
        with db.begin_nested():
            hooks = db.scalars(
                select(Webhook).where(Webhook.project_id == project_id, Webhook.enabled.is_(True))
            ).all()
            rows = []
            for hook in hooks:
                if event not in (hook.events or []):
                    continue
                delivery_id = new_id()
                row = WebhookDelivery(
                    id=delivery_id,
                    webhook_id=hook.id,
                    project_id=project_id,
                    event=event,
                    payload={
                        "id": delivery_id,
                        "event": event,
                        "created_at": now.isoformat(),
                        "project_id": project_id,
                        "data": data,
                    },
                    status="pending",
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                )
                db.add(row)
                rows.append(row)
            db.flush()
        return rows
    except Exception:  # events must never break the write that triggered them
        log.exception("could not enqueue webhook event %s", event)
        return []
