"""Backfill: run a rule over traces that already exist (FR-J3), capped by `eval_backfill_max`."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from traceloom.container import Container
from traceloom.db.models_eval import EvalRule
from traceloom.evals import rules
from traceloom.evals.rules import OBSERVATION, RuleSnapshot
from traceloom.ports import TraceQuery

log = logging.getLogger("traceloom.evals")

PAGE = 100


class RuleNotFound(LookupError):
    pass


@dataclass(frozen=True)
class BackfillResult:
    created: int  # new executions (matches that already had one are not counted)
    capped: bool  # stopped at the cap; more matches may remain


def rule_query(rule: RuleSnapshot, from_time: datetime | None, to_time: datetime | None, cursor):
    f = rule.filter
    return TraceQuery(
        name=f.get("name") if rule.target == "trace" else None,
        user_id=f.get("user_id"),
        session_id=f.get("session_id"),
        environment=f.get("environment"),
        release=f.get("release"),
        version=f.get("version"),
        tags=tuple(f.get("tags") or ()),
        min_level=f.get("min_level"),
        has_error=f.get("has_error"),
        metadata={k: str(v) for k, v in (f.get("metadata") or {}).items()},
        from_time=from_time,
        to_time=to_time,
        cursor=cursor,
        limit=PAGE,
    )


def backfill(
    container: Container,
    project_id: str,
    rule_id: str,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
) -> BackfillResult:
    """Create executions for the rule's existing matches in the time range, newest first, at most
    `eval_backfill_max` new ones. Matches that already have an execution are skipped, so running
    again after a capped run continues where it stopped."""
    with container.session_factory() as db:
        row = db.scalar(
            select(EvalRule).where(EvalRule.id == rule_id, EvalRule.project_id == project_id)
        )
        if row is None:
            raise RuleNotFound(rule_id)
        version_id = rules.resolve_version_id(db, row)
        if version_id is None:
            return BackfillResult(0, False)
        snapshot = RuleSnapshot(
            row.id,
            project_id,
            row.target,
            row.target_name,
            row.target_type,
            row.filter or {},
            row.sampling_pct,
            0,
            version_id,
        )
    limit, created, cursor = container.settings.eval_backfill_max, 0, None
    store = container.trace_store
    while True:
        page = store.list_traces(project_id, rule_query(snapshot, from_time, to_time, cursor))
        for trace in page.items:
            if snapshot.target == OBSERVATION:
                detail = store.get_trace(project_id, trace.id)
                candidates = detail.observations if detail else []
            else:
                candidates = [trace]
            created += rules.trigger(
                container, [snapshot], candidates, delay_seconds=0, limit=limit - created
            )
            if created >= limit:
                return BackfillResult(created, capped=True)
        if page.next_cursor is None:
            return BackfillResult(created, capped=False)
        cursor = page.next_cursor
