"""Annotation queues (FR-A1, A2, A5): create, edit, delete, add items, add from a filter, stats.

Every function takes the project and the acting `Actor`, scopes each query to the project and
checks the permission, so the web pages and any later API behave the same. Writes flush and add
audit rows; the caller commits.
"""

from __future__ import annotations

import statistics
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from traceloom.auth.permissions import Perm
from traceloom.config import Settings
from traceloom.db.base import new_id, utcnow
from traceloom.db.models import AnnotationItem, AnnotationQueue, Project, ScoreConfig, User
from traceloom.ports import TraceQuery, TraceStore
from traceloom.prompts.service import Actor
from traceloom.services import audit

OBJECT_TYPES = ("trace", "observation", "session")
STATUSES = ("pending", "skipped", "completed")
_SCORE_NAMESPACE = uuid.UUID("6f1c4a0e-8d52-4b8e-9f43-2a6f0d1b7c11")
SUMMARY_CHUNK = 500
NAME_MAX = 200
DESCRIPTION_MAX = 2000
MAX_CONFIGS = 20
PAGE_MAX = 200


class QueueError(ValueError):
    """The request is invalid; the message is safe to show."""


class QueueNotFound(LookupError):
    pass


@dataclass(frozen=True)
class ItemRef:
    object_type: str
    object_id: str
    trace_id: str | None = None  # the trace an observation belongs to


@dataclass(frozen=True)
class AddResult:
    added: int
    duplicates: int
    capped: bool = False  # more matches existed than the bulk limit allows
    total: int = 0  # items offered (after the cap)


@dataclass
class ReviewerStat:
    user_id: str
    name: str
    completed: int
    median_ms: float | None
    last_activity: datetime | None


@dataclass
class ScoreSummary:
    """The annotation scores a queue has written: how many, and one (label, value) per config."""

    total: int = 0
    lines: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class QueueStats:
    total: int = 0
    pending: int = 0
    completed: int = 0
    skipped: int = 0
    reviewers: list[ReviewerStat] = field(default_factory=list)

    @property
    def done(self) -> int:
        return self.completed + self.skipped

    @property
    def percent(self) -> int:
        return round(100 * self.completed / self.total) if self.total else 0

    @property
    def complete(self) -> bool:
        """Nothing left to review: every item completed (skipped ones can still be reopened)."""
        return self.total > 0 and self.pending == 0 and self.skipped == 0


# --- Helpers -----------------------------------------------------------------------------------


def _require(actor: Actor, perm: Perm, message: str) -> None:
    actor.require(perm, message)


def get_queue(db: Session, project: Project, actor: Actor, queue_id: str) -> AnnotationQueue:
    _require(actor, Perm.EVAL_READ, "You do not have access to annotation queues.")
    queue = db.scalar(
        select(AnnotationQueue).where(
            AnnotationQueue.id == queue_id, AnnotationQueue.project_id == project.id
        )
    )
    if queue is None:
        raise QueueNotFound(queue_id)
    return queue


def _audit(db: Session, project: Project, actor: Actor, action: str, queue: AnnotationQueue, **d):
    audit.record(
        db,
        action=action,
        actor_type=actor.type,
        actor_id=actor.id,
        org_id=project.org_id,
        project_id=project.id,
        target=queue.id,
        detail={"name": queue.name, **d},
    )


def _clean_configs(
    db: Session, project: Project, config_ids: Sequence[str], keep: Iterable[str] = ()
) -> list[str]:
    """Distinct ids of configs in this project. New ones must be active; `keep` lists ids the
    queue already has, which may stay even if archived."""
    ids = list(dict.fromkeys(c for c in config_ids if c))
    if not ids:
        raise QueueError("Choose at least one score config.")
    if len(ids) > MAX_CONFIGS:
        raise QueueError(f"A queue has at most {MAX_CONFIGS} score configs.")
    rows = {
        c.id: c
        for c in db.scalars(
            select(ScoreConfig).where(ScoreConfig.project_id == project.id, ScoreConfig.id.in_(ids))
        )
    }
    kept = set(keep)
    for config_id in ids:
        config = rows.get(config_id)
        if config is None:
            raise QueueError("Choose score configs from this project.")
        if config.archived_at is not None and config_id not in kept:
            raise QueueError(f"The score config {config.name!r} is archived.")
    return ids


def _clean_text(name: str, description: str) -> tuple[str, str]:
    name, description = (name or "").strip(), (description or "").strip()
    if not name or len(name) > NAME_MAX:
        raise QueueError(f"Give the queue a name of up to {NAME_MAX} characters.")
    if len(description) > DESCRIPTION_MAX:
        raise QueueError(f"The description is at most {DESCRIPTION_MAX} characters.")
    return name, description


# --- Queues ------------------------------------------------------------------------------------


def create_queue(
    db: Session,
    project: Project,
    actor: Actor,
    name: str,
    description: str = "",
    config_ids: Sequence[str] = (),
) -> AnnotationQueue:
    _require(actor, Perm.QUEUES_WRITE, "You need the Member role to create queues.")
    name, description = _clean_text(name, description)
    queue = AnnotationQueue(
        project_id=project.id,
        name=name,
        description=description,
        config_ids=_clean_configs(db, project, config_ids),
        created_by=actor.id,
    )
    db.add(queue)
    db.flush()
    _audit(db, project, actor, "annotation_queue.create", queue)
    return queue


def update_queue(
    db: Session,
    project: Project,
    actor: Actor,
    queue_id: str,
    name: str,
    description: str = "",
    config_ids: Sequence[str] = (),
) -> AnnotationQueue:
    _require(actor, Perm.QUEUES_WRITE, "You need the Member role to edit queues.")
    queue = get_queue(db, project, actor, queue_id)
    queue.name, queue.description = _clean_text(name, description)
    queue.config_ids = _clean_configs(db, project, config_ids, keep=queue.config_ids or [])
    db.flush()
    _audit(db, project, actor, "annotation_queue.update", queue)
    return queue


def delete_queue(db: Session, project: Project, actor: Actor, queue_id: str) -> None:
    _require(actor, Perm.QUEUES_DELETE, "Only Admins can delete queues.")
    queue = get_queue(db, project, actor, queue_id)
    items = db.scalar(
        select(func.count()).select_from(AnnotationItem).where(AnnotationItem.queue_id == queue.id)
    )
    _audit(db, project, actor, "annotation_queue.delete", queue, items=items or 0)
    db.delete(queue)  # its items go with it (ON DELETE CASCADE); written scores stay
    db.flush()


def queue_configs(db: Session, project: Project, queue: AnnotationQueue) -> list[ScoreConfig]:
    """The queue's score configs in the order the queue lists them (archived ones included)."""
    rows = {
        c.id: c
        for c in db.scalars(
            select(ScoreConfig).where(
                ScoreConfig.project_id == project.id, ScoreConfig.id.in_(queue.config_ids or [])
            )
        )
    }
    return [rows[i] for i in queue.config_ids or [] if i in rows]


def list_queues(
    db: Session, project: Project, actor: Actor
) -> list[tuple[AnnotationQueue, QueueStats]]:
    """Every queue of the project with its counts (no per-reviewer detail)."""
    _require(actor, Perm.EVAL_READ, "You do not have access to annotation queues.")
    queues = list(
        db.scalars(
            select(AnnotationQueue)
            .where(AnnotationQueue.project_id == project.id)
            .order_by(AnnotationQueue.created_at.desc(), AnnotationQueue.id)
        )
    )
    counts = _counts(db, project.id, [q.id for q in queues])
    return [(q, counts.get(q.id, QueueStats())) for q in queues]


def _counts(db: Session, project_id: str, queue_ids: Sequence[str]) -> dict[str, QueueStats]:
    out: dict[str, QueueStats] = {}
    if not queue_ids:
        return out
    rows = db.execute(
        select(AnnotationItem.queue_id, AnnotationItem.status, func.count())
        .where(AnnotationItem.project_id == project_id, AnnotationItem.queue_id.in_(queue_ids))
        .group_by(AnnotationItem.queue_id, AnnotationItem.status)
    )
    for queue_id, status, n in rows:
        stats = out.setdefault(queue_id, QueueStats())
        stats.total += n
        if status in STATUSES:
            setattr(stats, status, getattr(stats, status) + n)
    return out


# --- Items -------------------------------------------------------------------------------------


def _insert_ignore(db: Session):
    dialect = postgresql if db.get_bind().dialect.name == "postgresql" else sqlite
    return dialect.insert(AnnotationItem).on_conflict_do_nothing(
        index_elements=["queue_id", "object_type", "object_id"]
    )


def add_items(
    db: Session,
    project: Project,
    actor: Actor,
    queue_id: str,
    refs: Iterable[ItemRef],
    settings: Settings,
    *,
    offered: int | None = None,
) -> AddResult:
    """Add objects to the queue. Ones already in it (or repeated in `refs`) count as duplicates.
    At most `queue_bulk_max` are added per call; `offered` is how many matched before the cap."""
    _require(actor, Perm.QUEUES_WRITE, "You need the Member role to add items to a queue.")
    queue = get_queue(db, project, actor, queue_id)
    unique: dict[tuple[str, str], ItemRef] = {}
    repeated = 0
    for ref in refs:
        if ref.object_type not in OBJECT_TYPES or not ref.object_id:
            raise QueueError("Items are traces, observations or sessions.")
        if ref.object_type == "observation" and not ref.trace_id:
            raise QueueError("An observation can only be added together with its trace id.")
        key = (ref.object_type, ref.object_id)
        if key in unique:
            repeated += 1
        else:
            unique[key] = ref
    limit = settings.queue_bulk_max
    chosen = list(unique.values())[:limit]
    capped = len(unique) > limit or (offered is not None and offered > limit)
    added, now = 0, utcnow()
    for n, ref in enumerate(chosen):
        trace_id = ref.trace_id or (ref.object_id if ref.object_type == "trace" else None)
        result = db.execute(
            _insert_ignore(db).values(
                id=new_id(),
                queue_id=queue.id,
                project_id=project.id,
                object_type=ref.object_type,
                object_id=ref.object_id,
                trace_id=trace_id,
                status="pending",
                created_at=now + timedelta(microseconds=n),  # keeps the order they were added
            )
        )
        added += result.rowcount or 0
    db.flush()
    duplicates = len(chosen) - added + repeated
    _audit(
        db, project, actor, "annotation_queue.add_items", queue, added=added, duplicates=duplicates
    )
    return AddResult(added, duplicates, capped, len(chosen))


def add_from_filter(
    db: Session,
    project: Project,
    actor: Actor,
    queue_id: str,
    store: TraceStore,
    query: TraceQuery,
    settings: Settings,
) -> AddResult:
    """Snapshot every trace matching `query` into the queue (capped by `queue_bulk_max`)."""
    _require(actor, Perm.QUEUES_WRITE, "You need the Member role to add items to a queue.")
    get_queue(db, project, actor, queue_id)
    ids = store.matching_trace_ids(project.id, query)
    refs = [ItemRef("trace", trace_id, trace_id) for trace_id in ids[: settings.queue_bulk_max]]
    return add_items(db, project, actor, queue_id, refs, settings, offered=len(ids))


def get_item(db: Session, project: Project, actor: Actor, queue_id: str, item_id: str):
    _require(actor, Perm.EVAL_READ, "You do not have access to annotation queues.")
    item = db.scalar(
        select(AnnotationItem).where(
            AnnotationItem.id == item_id,
            AnnotationItem.queue_id == queue_id,
            AnnotationItem.project_id == project.id,
        )
    )
    if item is None:
        raise QueueNotFound(item_id)
    return item


def list_items(
    db: Session,
    project: Project,
    actor: Actor,
    queue_id: str,
    *,
    status: str | None = None,
    reviewer: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AnnotationItem], int]:
    """A page of items, oldest first, and the total matching the filters. `reviewer` is the user
    who completed the item or holds its lock."""
    get_queue(db, project, actor, queue_id)
    where: list[Any] = [
        AnnotationItem.queue_id == queue_id,
        AnnotationItem.project_id == project.id,
    ]
    if status:
        if status not in STATUSES:
            raise QueueError("Unknown status.")
        where.append(AnnotationItem.status == status)
    if reviewer:
        where.append(
            (AnnotationItem.completed_by == reviewer) | (AnnotationItem.locked_by == reviewer)
        )
    total = db.scalar(select(func.count()).select_from(AnnotationItem).where(*where)) or 0
    items = db.scalars(
        select(AnnotationItem)
        .where(*where)
        .order_by(AnnotationItem.created_at, AnnotationItem.id)
        .limit(max(1, min(limit, PAGE_MAX)))
        .offset(max(0, offset))
    )
    return list(items), total


def position(db: Session, item: AnnotationItem) -> int:
    """1-based place of the item in its queue's order (oldest first)."""
    earlier = db.scalar(
        select(func.count())
        .select_from(AnnotationItem)
        .where(
            AnnotationItem.queue_id == item.queue_id,
            (AnnotationItem.created_at < item.created_at)
            | ((AnnotationItem.created_at == item.created_at) & (AnnotationItem.id <= item.id)),
        )
    )
    return earlier or 1


def last_activity(db: Session, queue_id: str) -> tuple[str, datetime] | None:
    """Who completed an item last and when, for the queue list."""
    row = db.execute(
        select(AnnotationItem.completed_by, AnnotationItem.completed_at)
        .where(AnnotationItem.queue_id == queue_id, AnnotationItem.status == "completed")
        .order_by(AnnotationItem.completed_at.desc())
        .limit(1)
    ).first()
    if row is None or row[1] is None:
        return None
    user = db.get(User, row[0]) if row[0] else None
    return ((user.name or user.email) if user else "Former member", row[1])


# --- Stats -------------------------------------------------------------------------------------


def stats(db: Session, project: Project, actor: Actor, queue_id: str) -> QueueStats:
    """Counts, and per reviewer: completed items, median time per item and last activity."""
    queue = get_queue(db, project, actor, queue_id)
    result = _counts(db, project.id, [queue.id]).get(queue.id, QueueStats())
    rows = db.execute(
        select(
            AnnotationItem.completed_by, AnnotationItem.duration_ms, AnnotationItem.completed_at
        ).where(
            AnnotationItem.queue_id == queue.id,
            AnnotationItem.status == "completed",
            AnnotationItem.completed_by.is_not(None),
        )
    ).all()
    by_user: dict[str, list[tuple[int | None, datetime | None]]] = {}
    for user_id, duration, at in rows:
        by_user.setdefault(user_id, []).append((duration, at))
    names = {
        u.id: (u.name or u.email)
        for u in db.scalars(select(User).where(User.id.in_(list(by_user) or [""])))
    }
    for user_id, entries in by_user.items():
        durations = [d for d, _ in entries if d is not None]
        times = [t for _, t in entries if t is not None]
        result.reviewers.append(
            ReviewerStat(
                user_id,
                names.get(user_id, "Former member"),
                len(entries),
                statistics.median(durations) if durations else None,
                max(times) if times else None,
            )
        )
    result.reviewers.sort(key=lambda r: (-r.completed, r.name))
    return result


# --- Scores written ----------------------------------------------------------------------------


def score_id_for(item_id: str, config_id: str) -> str:
    """The id of the score a completed item wrote for one config (stable, so a retry
    overwrites instead of duplicating)."""
    return uuid.uuid5(_SCORE_NAMESPACE, f"{item_id}:{config_id}").hex


def _fmt(number: float) -> str:
    return f"{number:.2f}".rstrip("0").rstrip(".") if number % 1 else str(int(number))


def score_summary(
    db: Session, store: TraceStore, project: Project, actor: Actor, queue_id: str
) -> ScoreSummary:
    """What the queue's completed items wrote, read back through the trace store: numeric
    configs give a mean, boolean ones how many are true, categorical ones the most common
    label. Scores deleted since are not counted."""
    queue = get_queue(db, project, actor, queue_id)
    item_ids = list(
        db.scalars(
            select(AnnotationItem.id).where(
                AnnotationItem.queue_id == queue.id,
                AnnotationItem.project_id == project.id,
                AnnotationItem.status == "completed",
            )
        )
    )
    configs = queue_configs(db, project, queue)
    wanted = {score_id_for(i, c.id): c.id for i in item_ids for c in configs}
    rows: list[Any] = []
    ids = list(wanted)
    for start in range(0, len(ids), SUMMARY_CHUNK):
        found = store.get_existing(project.id, {"score": ids[start : start + SUMMARY_CHUNK]})
        rows.extend(found.get("score", {}).values())
    by_config: dict[str, list[Any]] = {}
    for row in rows:
        by_config.setdefault(wanted[row.id], []).append(row)
    lines: list[tuple[str, str]] = []
    for config in configs:
        got = by_config.get(config.id, [])
        if not got:
            continue
        if config.data_type == "NUMERIC":
            values = [r.value for r in got if r.value is not None]
            if values:
                lines.append((f"{config.name}, mean", _fmt(statistics.fmean(values))))
        elif config.data_type == "BOOLEAN":
            true = sum(1 for r in got if r.value)
            lines.append((f"{config.name}, true", f"{true} of {len(got)}"))
        else:
            labels = [r.string_value for r in got if r.string_value]
            if labels:
                top = max(set(labels), key=lambda label: (labels.count(label), label))
                lines.append(
                    (f"{config.name}, most common", f"{top} ({labels.count(top)} of {len(got)})")
                )
    return ScoreSummary(len(rows), lines)
