"""Item locks and review outcomes for annotation queues (FR-A3, A4, NFR-2).

A claim is one conditional UPDATE, so two reviewers can never hold the same item:
- `claim_next` takes the oldest pending item that is unlocked, has an expired lock, or is already
  held by this reviewer. On Postgres the candidate is picked with `FOR UPDATE SKIP LOCKED` inside
  the UPDATE; on SQLite the statement is atomic on its own, with a bounded retry in case the
  picked row is taken between statements.
- `claim_item` opens a chosen item with the same rule.
Holding an item renews its lock at each save; completing, skipping or releasing frees it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from traceloom.annotation.queues import get_item, get_queue
from traceloom.auth.permissions import Perm
from traceloom.config import Settings
from traceloom.db.base import utcnow
from traceloom.db.models import AnnotationItem, AnnotationQueue, Project, ScoreConfig, User
from traceloom.ports import TraceStore
from traceloom.prompts.service import Actor
from traceloom.scores import service as score_service
from traceloom.scores.configs import ConfigSnapshot, check_score

CLAIM_RETRIES = 5
MAX_DURATION_MS = 24 * 3600 * 1000
_NAMESPACE = uuid.UUID("6f1c4a0e-8d52-4b8e-9f43-2a6f0d1b7c11")


class ClaimError(ValueError):
    """The action cannot be done; the message is safe to show."""


@dataclass(frozen=True)
class Opened:
    """What opening an item gave: it is `mine` (locked for this reviewer), `locked` by someone
    else, or `closed` (completed or skipped; read-only)."""

    item: AnnotationItem
    state: str  # mine | locked | closed
    locked_by_name: str = ""

    @property
    def read_only(self) -> bool:
        return self.state != "mine"


def _require_review(actor: Actor) -> None:
    actor.require(Perm.QUEUES_WRITE, "You need the Member role to review queue items.")


def _free_for(user_id: str, now: datetime):
    """The item is not held by anyone else: unlocked, lock expired, or held by `user_id`."""
    return or_(
        AnnotationItem.locked_by.is_(None),
        AnnotationItem.locked_until.is_(None),
        AnnotationItem.locked_until <= now,
        AnnotationItem.locked_by == user_id,
    )


def _lock_for(settings: Settings, now: datetime) -> datetime:
    return now + timedelta(minutes=settings.annotation_lock_minutes)


def _locker_name(db: Session, item: AnnotationItem) -> str:
    user = db.get(User, item.locked_by) if item.locked_by else None
    return (user.name or user.email) if user else "another reviewer"


# --- Claim -------------------------------------------------------------------------------------


def claim_next(
    db: Session,
    project: Project,
    actor: Actor,
    queue_id: str,
    settings: Settings,
    now: datetime | None = None,
) -> AnnotationItem | None:
    """Atomically lock the oldest pending item available to this reviewer; None when there is none.
    An item the reviewer already holds is returned first, so reloading never strands it."""
    _require_review(actor)
    queue = get_queue(db, project, actor, queue_id)
    now = now or utcnow()
    until = _lock_for(settings, now)
    bind = db.get_bind().dialect.name
    for _ in range(CLAIM_RETRIES):
        candidate = (
            select(AnnotationItem.id)
            .where(
                AnnotationItem.queue_id == queue.id,
                AnnotationItem.project_id == project.id,
                AnnotationItem.status == "pending",
                _free_for(actor.id, now),
            )
            .order_by(
                (AnnotationItem.locked_by == actor.id).desc(),
                AnnotationItem.created_at,
                AnnotationItem.id,
            )
            .limit(1)
        )
        if bind == "postgresql":
            candidate = candidate.with_for_update(skip_locked=True)
        claimed = db.execute(
            update(AnnotationItem)
            .where(
                AnnotationItem.id == candidate.scalar_subquery(),
                AnnotationItem.status == "pending",
                _free_for(actor.id, now),
            )
            .values(locked_by=actor.id, locked_until=until)
            .returning(AnnotationItem.id)
            .execution_options(synchronize_session=False)
        ).scalar()
        if claimed is not None:
            db.flush()
            item = db.get(AnnotationItem, claimed, populate_existing=True)
            return item
        # Nothing was claimed: either no item is free, or the pick was taken first. Retry only
        # when a free item still exists.
        if db.scalar(candidate) is None:
            return None
    return None


def claim_item(
    db: Session,
    project: Project,
    actor: Actor,
    queue_id: str,
    item_id: str,
    settings: Settings,
    now: datetime | None = None,
) -> Opened:
    """Open one item. A pending item that is free is locked for this reviewer; one held by someone
    else comes back read-only with the holder's name; completed and skipped items are read-only."""
    actor.require(Perm.EVAL_READ, "You do not have access to annotation queues.")
    item = get_item(db, project, actor, queue_id, item_id)
    now = now or utcnow()
    if item.status != "pending":
        return Opened(item, "closed")
    if not actor.can(Perm.QUEUES_WRITE):
        return Opened(
            item,
            "locked" if _held_by_other(item, actor.id, now) else "closed",
            _locker_name(db, item),
        )
    taken = db.execute(
        update(AnnotationItem)
        .where(
            AnnotationItem.id == item.id,
            AnnotationItem.status == "pending",
            _free_for(actor.id, now),
        )
        .values(locked_by=actor.id, locked_until=_lock_for(settings, now))
        .execution_options(synchronize_session=False)
    ).rowcount
    db.flush()
    db.refresh(item)
    if taken:
        return Opened(item, "mine")
    return Opened(item, "locked", _locker_name(db, item))


def _held_by_other(item: AnnotationItem, user_id: str, now: datetime) -> bool:
    return bool(
        item.locked_by
        and item.locked_by != user_id
        and item.locked_until
        and item.locked_until > now
    )


def renew(
    db: Session,
    project: Project,
    actor: Actor,
    queue_id: str,
    item_id: str,
    settings: Settings,
    now: datetime | None = None,
) -> bool:
    """Extend the lock this reviewer holds. False when they no longer hold it."""
    _require_review(actor)
    get_item(db, project, actor, queue_id, item_id)
    now = now or utcnow()
    renewed = db.execute(
        update(AnnotationItem)
        .where(
            AnnotationItem.id == item_id,
            AnnotationItem.status == "pending",
            AnnotationItem.locked_by == actor.id,
            or_(AnnotationItem.locked_until.is_(None), AnnotationItem.locked_until > now),
        )
        .values(locked_until=_lock_for(settings, now))
        .execution_options(synchronize_session=False)
    ).rowcount
    return bool(renewed)


def release(db: Session, project: Project, actor: Actor, queue_id: str, item_id: str) -> bool:
    """Give up the lock without changing the item's status."""
    _require_review(actor)
    get_item(db, project, actor, queue_id, item_id)
    freed = db.execute(
        update(AnnotationItem)
        .where(AnnotationItem.id == item_id, AnnotationItem.locked_by == actor.id)
        .values(locked_by=None, locked_until=None)
        .execution_options(synchronize_session=False)
    ).rowcount
    return bool(freed)


# --- Outcomes ----------------------------------------------------------------------------------


def _close(
    db: Session,
    item_id: str,
    actor: Actor,
    status: str,
    now: datetime,
    duration_ms: int | None,
) -> None:
    """Move a pending item this reviewer may act on to `status`, or raise ClaimError. An expired
    lock still lets the last holder finish unless someone else has claimed it since."""
    done = db.execute(
        update(AnnotationItem)
        .where(
            AnnotationItem.id == item_id,
            AnnotationItem.status == "pending",
            or_(
                AnnotationItem.locked_by.is_(None),
                AnnotationItem.locked_by == actor.id,
                AnnotationItem.locked_until <= now,
            ),
        )
        .values(
            status=status,
            locked_by=None,
            locked_until=None,
            completed_by=actor.id if status == "completed" else None,
            completed_at=now if status == "completed" else None,
            duration_ms=duration_ms if status == "completed" else None,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if not done:
        raise ClaimError("This item is locked by someone else or is already done.")


def _may_finish(item: AnnotationItem, actor: Actor, now: datetime) -> bool:
    """Same rule as `_close`, checked up front so scores are not written for a lost item."""
    if item.status != "pending":
        return False
    return not _held_by_other(item, actor.id, now)


def _clamp_duration(duration_ms: int | None) -> int | None:
    if duration_ms is None:
        return None
    return max(0, min(int(duration_ms), MAX_DURATION_MS))


def check_values(
    db: Session, project: Project, queue: AnnotationQueue, values: Mapping[str, Any]
) -> list[ScoreConfig]:
    """The queue's configs, after checking that `values` has one valid value for each."""
    configs = list(
        db.scalars(
            select(ScoreConfig).where(
                ScoreConfig.project_id == project.id, ScoreConfig.id.in_(queue.config_ids or [])
            )
        )
    )
    order = {cid: i for i, cid in enumerate(queue.config_ids or [])}
    configs.sort(key=lambda c: order.get(c.id, 0))
    for config in configs:
        if config.id not in values or values[config.id] in (None, ""):
            raise ClaimError(f"Score {config.name!r}, or skip the item.")
        problem = check_score(ConfigSnapshot.of(config), values[config.id])
        if problem:
            raise ClaimError(problem[0].upper() + problem[1:] + ".")
    return configs


def complete(
    db: Session,
    store: TraceStore,
    project: Project,
    actor: Actor,
    queue_id: str,
    item_id: str,
    values: Mapping[str, Any],
    *,
    comment: str = "",
    duration_ms: int | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Write one ANNOTATION score per queue config (tagged with the queue and the reviewer),
    then mark the item completed and release the lock. Returns the score ids.

    Score ids derive from the item and config, so completing again overwrites instead of
    duplicating. The caller's pending work is committed first: in lite mode the trace store
    writes to the same database file, and an open write would lock it out."""
    _require_review(actor)
    queue = get_queue(db, project, actor, queue_id)
    item = get_item(db, project, actor, queue_id, item_id)
    configs = check_values(db, project, queue, values)
    comment = (comment or "").strip()[:5000]
    now = now or utcnow()
    db.commit()  # release any write lock before the store writes (see the docstring)
    if not _may_finish(item, actor, now):
        raise ClaimError("This item is locked by someone else or is already done.")
    where: dict[str, Any]
    if item.object_type == "session":
        where = {"session_id": item.object_id}
    elif item.object_type == "observation":
        where = {"trace_id": item.trace_id, "observation_id": item.object_id}
    else:
        where = {"trace_id": item.object_id}
    ids = []
    for config in configs:
        score_id = uuid.uuid5(_NAMESPACE, f"{item.id}:{config.id}").hex
        score_service.create_score(
            store,
            project.id,
            name=config.name,
            value=values[config.id],
            source="ANNOTATION",
            config_id=config.id,
            data_type=config.data_type,
            score_id=score_id,
            timestamp=now,
            author_id=actor.id,
            queue_id=queue.id,
            comment=comment or None,
            **where,
        )
        ids.append(score_id)
    _close(db, item.id, actor, "completed", now, _clamp_duration(duration_ms))
    return ids


def skip(
    db: Session,
    project: Project,
    actor: Actor,
    queue_id: str,
    item_id: str,
    now: datetime | None = None,
) -> None:
    _require_review(actor)
    get_queue(db, project, actor, queue_id)
    get_item(db, project, actor, queue_id, item_id)
    _close(db, item_id, actor, "skipped", now or utcnow(), None)


def reopen(db: Session, project: Project, actor: Actor, queue_id: str, item_id: str) -> None:
    """Put a skipped item back to pending, unlocked."""
    _require_review(actor)
    get_queue(db, project, actor, queue_id)
    get_item(db, project, actor, queue_id, item_id)
    done = db.execute(
        update(AnnotationItem)
        .where(AnnotationItem.id == item_id, AnnotationItem.status == "skipped")
        .values(status="pending", locked_by=None, locked_until=None)
        .execution_options(synchronize_session=False)
    ).rowcount
    if not done:
        raise ClaimError("Only skipped items can be reopened.")
