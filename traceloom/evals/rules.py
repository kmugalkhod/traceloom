"""Online judge rules: matching at ingest, sampling, idempotent jobs, and the sweeper's source
(FR-J3, J4; ADR-0014).

After an ingest batch commits, `on_ingested` tests every enabled rule of the project against the
merged rows and records one `eval_executions` row per (rule, object). Rows are the source of truth:
the delayed `eval_job` message is only a wake-up, and `due_messages` lets the sweeper re-send a
lost one. Matching never raises into ingestion.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, text, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from traceloom.config import Settings
from traceloom.container import Container
from traceloom.db.base import new_id
from traceloom.db.models_eval import EvalExecution, EvalRule, EvalTemplateVersion
from traceloom.evals import spend
from traceloom.ports import ObservationRow, TraceRow
from traceloom.queue_registry import EVAL_JOB

log = logging.getLogger("traceloom.evals")

TRACE, OBSERVATION = "trace", "observation"
SAMPLE_BUCKETS = 10_000
# A pending job is re-sent only once it is this overdue, so the normal delayed message gets a
# chance to arrive first.
DUE_GRACE_SECONDS = 30
_CACHE_KEY = "eval_rules:{}"


# --- Rule snapshots (cached per project) -------------------------------------------------------


@dataclass(frozen=True)
class RuleSnapshot:
    id: str
    project_id: str
    target: str  # trace | observation
    target_name: str | None
    target_type: str | None
    filter: Mapping[str, Any]
    sampling_pct: float
    delay_seconds: int
    template_version_id: str  # the pinned version, or the latest one when the rule follows it


def resolve_version_id(db: Session, rule: EvalRule) -> str | None:
    query = select(EvalTemplateVersion.id).where(
        EvalTemplateVersion.template_id == rule.template_id
    )
    if rule.template_version is not None:
        query = query.where(EvalTemplateVersion.version == rule.template_version)
    return db.scalar(query.order_by(EvalTemplateVersion.version.desc()).limit(1))


def load_rules(container: Container, project_id: str) -> list[RuleSnapshot]:
    """Enabled rules of the project, cached for `eval_rule_cache_seconds`."""
    ttl = container.settings.eval_rule_cache_seconds
    key = _CACHE_KEY.format(project_id)
    if ttl > 0 and (cached := container.cache.get(key)) is not None:
        return [RuleSnapshot(**item) for item in cached]
    with container.session_factory() as db:
        rules = []
        for rule in db.scalars(
            select(EvalRule).where(EvalRule.project_id == project_id, EvalRule.enabled.is_(True))
        ):
            version_id = resolve_version_id(db, rule)
            if version_id is None:
                continue  # a template without versions cannot run
            rules.append(
                RuleSnapshot(
                    rule.id,
                    project_id,
                    rule.target,
                    rule.target_name,
                    rule.target_type,
                    rule.filter or {},
                    rule.sampling_pct,
                    rule.delay_seconds,
                    version_id,
                )
            )
    if ttl > 0:
        container.cache.set(key, [asdict(r) for r in rules], ttl)
    return rules


def invalidate_rules(container: Container, project_id: str) -> None:
    """Call after a rule or template changes so the next batch sees it."""
    container.cache.delete(_CACHE_KEY.format(project_id))


# --- Matching ----------------------------------------------------------------------------------


def is_sampled(rule_id: str, object_id: str, sampling_pct: float) -> bool:
    """Deterministic: the same (rule, object) always gets the same answer."""
    digest = hashlib.sha256(f"{rule_id}:{object_id}".encode()).hexdigest()
    return int(digest[:8], 16) % SAMPLE_BUCKETS < sampling_pct * (SAMPLE_BUCKETS / 100)


def excluded_environments(settings: Settings) -> frozenset[str]:
    """Judge and experiment traces never trigger rules."""
    return frozenset({settings.eval_environment, settings.experiment_environment})


# Filter keys the in-memory row can answer. Others (scores, latency, cost, level, ...) are
# re-checked against the store when the job runs.
_ROW_FIELDS = ("name", "user_id", "session_id", "environment", "release", "version")


def _filter_matches(rule_filter: Mapping[str, Any], row: TraceRow | ObservationRow) -> bool:
    for key in _ROW_FIELDS:
        wanted = rule_filter.get(key)
        if wanted is not None and getattr(row, key, None) != wanted:
            return False
    tags = rule_filter.get("tags")
    if tags and not set(tags) & set(getattr(row, "tags", ()) or ()):
        return False
    metadata = row.metadata or {}
    for key, value in (rule_filter.get("metadata") or {}).items():
        if key not in metadata or str(metadata[key]) != str(value):
            return False
    return True


def matches(rule: RuleSnapshot, row: TraceRow | ObservationRow) -> bool:
    """Target, then the filter predicates the row can answer."""
    if isinstance(row, ObservationRow):
        if rule.target != OBSERVATION:
            return False
        if rule.target_name and row.name != rule.target_name:
            return False
        if rule.target_type and (row.type or "").upper() != rule.target_type.upper():
            return False
    elif rule.target != TRACE:
        return False
    return _filter_matches(rule.filter, row)


# --- Executions --------------------------------------------------------------------------------


def create_execution(
    db: Session,
    rule: RuleSnapshot,
    row: TraceRow | ObservationRow,
    now: datetime,
    delay_seconds: float | None = None,
) -> str | None:
    """Insert the execution unless this (rule, object) already has one. Returns the new id, or
    None when it existed. The caller commits and enqueues."""
    is_observation = isinstance(row, ObservationRow)
    delay = rule.delay_seconds if delay_seconds is None else delay_seconds
    insert = (postgresql if db.get_bind().dialect.name == "postgresql" else sqlite).insert
    execution_id = new_id()
    created = db.execute(
        insert(EvalExecution)
        .values(
            id=execution_id,
            project_id=rule.project_id,
            rule_id=rule.id,
            template_version_id=rule.template_version_id,
            object_type=OBSERVATION if is_observation else TRACE,
            object_id=row.id,
            trace_id=row.trace_id if is_observation else row.id,
            status="pending",
            attempts=0,
            estimated_cost=0.0,
            run_after=now + timedelta(seconds=delay),
            created_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=["rule_id", "object_type", "object_id"],
            index_where=text("rule_id IS NOT NULL"),
        )
    ).rowcount
    return execution_id if created else None


def trigger(
    container: Container,
    rules: Sequence[RuleSnapshot],
    rows: Iterable[TraceRow | ObservationRow],
    *,
    delay_seconds: float | None = None,
    limit: int | None = None,
) -> int:
    """Create (and enqueue) executions for every sampled (rule, row) match; at most `limit`.
    Returns how many were created."""
    skip = excluded_environments(container.settings)
    now, created = datetime.now(UTC), 0
    for row in rows:
        if limit is not None and created >= limit:
            break
        if row.environment in skip:
            continue
        for rule in rules:
            if limit is not None and created >= limit:
                break
            if not matches(rule, row) or not is_sampled(rule.id, row.id, rule.sampling_pct):
                continue
            try:
                with container.session_factory.begin() as db:
                    execution_id = create_execution(db, rule, row, now, delay_seconds)
            except Exception:  # one bad rule must not stop the others
                log.exception("eval rule %s failed on %s", rule.id, row.id)
                continue
            if execution_id is None:
                continue
            created += 1
            delay = rule.delay_seconds if delay_seconds is None else delay_seconds
            _enqueue(container, execution_id, delay)
    return created


def _enqueue(container: Container, execution_id: str, delay: float) -> None:
    try:
        container.queue.enqueue({"type": EVAL_JOB, "execution_id": execution_id}, delay)
    except Exception:  # the row exists; the sweeper re-sends the wake-up
        log.exception("could not enqueue eval job %s", execution_id)


def on_ingested(
    container: Container,
    project_id: str,
    traces: Sequence[TraceRow],
    observations: Sequence[ObservationRow],
) -> int:
    """Create judge jobs for the rows a committed batch touched. Safe to run again for the same
    batch: an existing (rule, object) execution is left alone."""
    rules = load_rules(container, project_id)
    if not rules:
        return 0
    return trigger(container, rules, [*traces, *observations])


# --- Sweeper source ----------------------------------------------------------------------------


def due_messages(container: Container) -> list[dict[str, Any]]:
    """`eval_job` messages for pending executions that are overdue, after resetting jobs stuck in
    `running` (their reservation is released) back to pending."""
    settings, sessions = container.settings, container.session_factory
    now = datetime.now(UTC)
    stuck_before = now - timedelta(seconds=settings.eval_stuck_seconds)
    releases: list[spend.Reservation] = []
    reset_ids: list[str] = []
    with sessions.begin() as db:
        stuck = db.scalars(
            select(EvalExecution)
            .where(EvalExecution.status == "running", EvalExecution.started_at < stuck_before)
            .limit(settings.eval_sweep_batch)
        ).all()
        for ex in stuck:
            # Read first: the ORM update below also rewrites this object in the session.
            project_id, started_at, held = ex.project_id, ex.started_at, ex.estimated_cost
            reset = db.execute(
                update(EvalExecution)
                .where(EvalExecution.id == ex.id, EvalExecution.status == "running")
                .values(status="pending", run_after=now, started_at=None, estimated_cost=0.0)
            ).rowcount
            if reset:
                reset_ids.append(ex.id)
            if reset and held > 0 and started_at is not None:
                releases.append(spend.Reservation(project_id, spend.utc_day(started_at), held))
        due_before = now - timedelta(seconds=DUE_GRACE_SECONDS)
        ids = db.scalars(
            select(EvalExecution.id)
            .where(
                EvalExecution.status == "pending",
                or_(EvalExecution.run_after.is_(None), EvalExecution.run_after <= due_before),
            )
            .order_by(EvalExecution.run_after)
            .limit(settings.eval_sweep_batch)
        ).all()
    for reservation in releases:  # after the reset commits: a dead job's hold is freed
        spend.release(sessions, reservation)
    return [{"type": EVAL_JOB, "execution_id": i} for i in [*reset_ids, *ids]]


JobSource = Callable[[], Iterable[Mapping[str, Any]]]
