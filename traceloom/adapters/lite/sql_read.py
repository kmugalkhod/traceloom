"""Read-side queries for the lite SQL TraceStore: list (filters, search, sort, cursor), detail.

The host class (SqlTraceStore) provides `session_factory`, a SQLAlchemy sessionmaker. Every
statement starts with a `project_id = :pid` predicate; there is no way to read without one.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Text, and_, cast, func, or_, select
from sqlalchemy.orm import Session

from traceloom.db.models import Observation, Score, Trace
from traceloom.ports import (
    LEVELS,
    InvalidCursor,
    ObservationRow,
    Page,
    ScoreRow,
    TraceDetail,
    TraceQuery,
    TraceRow,
    decode_cursor,
    encode_cursor,
)

MAX_PAGE_SIZE = 200
_LIKE_ESCAPE = "\\"


def _sort_column(sort: str) -> ColumnElement[Any]:
    # NULL latency sorts as -1 so keyset comparisons stay total.
    columns: dict[str, ColumnElement[Any]] = {
        "timestamp": Trace.timestamp,
        "latency": func.coalesce(Trace.latency_ms, -1.0),
        "cost": Trace.total_cost,
        "tokens": Trace.total_tokens,
    }
    try:
        return columns[sort]
    except KeyError:
        raise ValueError(f"unknown sort {sort!r}; expected one of {sorted(columns)}") from None


def _contains(column: ColumnElement[Any], needle: str) -> ColumnElement[bool]:
    escaped = (
        needle.lower()
        .replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return func.lower(column).like(f"%{escaped}%", escape=_LIKE_ESCAPE)


def trace_filter_clauses(project_id: str, query: TraceQuery) -> list[ColumnElement[bool]]:
    """WHERE clauses for a TraceQuery (without sort or cursor). project_id is always first."""
    clauses: list[ColumnElement[bool]] = [Trace.project_id == project_id]
    if query.q:
        clauses.append(
            or_(
                _contains(Trace.search_text, query.q),
                _contains(func.coalesce(Trace.name, ""), query.q),
                _contains(Trace.id, query.q),
            )
        )
    for column, value in (
        (Trace.name, query.name),
        (Trace.user_id, query.user_id),
        (Trace.session_id, query.session_id),
        (Trace.environment, query.environment),
        (Trace.release, query.release),
        (Trace.version, query.version),
    ):
        if value is not None:
            clauses.append(column == value)
    if query.tags:
        # tags is a JSON array of strings; match the JSON-encoded element.
        tags_text = cast(Trace.tags, Text)
        clauses.append(or_(*(_contains_json_string(tags_text, tag) for tag in query.tags)))
    if query.min_level:
        if query.min_level not in LEVELS:
            raise ValueError(f"unknown level {query.min_level!r}")
        clauses.append(Trace.max_level.in_(LEVELS[LEVELS.index(query.min_level) :]))
    if query.has_error is True:
        clauses.append(Trace.max_level == "ERROR")
    elif query.has_error is False:
        clauses.append(Trace.max_level != "ERROR")
    for key, value in query.metadata.items():
        clauses.append(Trace.metadata_[key].as_string() == str(value))
    _range(clauses, Trace.latency_ms, query.latency_min_ms, query.latency_max_ms)
    _range(clauses, Trace.total_cost, query.cost_min, query.cost_max)
    _range(clauses, Trace.timestamp, query.from_time, query.to_time)
    if query.bookmarked is not None:
        clauses.append(Trace.bookmarked == query.bookmarked)
    return clauses


def _contains_json_string(column_text: ColumnElement[Any], value: str) -> ColumnElement[bool]:
    encoded = json.dumps(value)  # the form the JSON column stores, quotes included
    escaped = (
        encoded.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return column_text.like(f"%{escaped}%", escape=_LIKE_ESCAPE)


def _range(
    clauses: list[ColumnElement[bool]], column: ColumnElement[Any], low: Any, high: Any
) -> None:
    if low is not None:
        clauses.append(column >= low)
    if high is not None:
        clauses.append(column <= high)


def _keyset(sort_col: ColumnElement[Any], descending: bool, cursor: str) -> ColumnElement[bool]:
    """Rows strictly after the cursor. Order is (sort_col, id asc) so id breaks ties."""
    value, row_id = decode_cursor(cursor)
    is_time = sort_col is Trace.timestamp
    if is_time != isinstance(value, datetime) or (
        not is_time and (isinstance(value, bool) or not isinstance(value, int | float))
    ):
        raise InvalidCursor("cursor does not match the sort")
    beyond = sort_col < value if descending else sort_col > value
    return or_(beyond, and_(sort_col == value, Trace.id > row_id))


def _loads(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text  # stored as plain text


def trace_to_row(t: Trace) -> TraceRow:
    return TraceRow(
        id=t.id,
        timestamp=t.timestamp,
        name=t.name,
        input=_loads(t.input),
        output=_loads(t.output),
        user_id=t.user_id,
        session_id=t.session_id,
        tags=list(t.tags or []),
        metadata=dict(t.metadata_ or {}),
        release=t.release,
        version=t.version,
        environment=t.environment,
        public=bool(t.public),
        bookmarked=bool(t.bookmarked),
        event_ts=t.event_ts,
        latency_ms=t.latency_ms,
        total_tokens=int(t.total_tokens or 0),
        total_cost=float(t.total_cost or 0.0),
        max_level=t.max_level,
        observation_count=int(t.observation_count or 0),
        search_text=t.search_text or "",
    )


def observation_to_row(o: Observation) -> ObservationRow:
    return ObservationRow(
        id=o.id,
        trace_id=o.trace_id,
        parent_observation_id=o.parent_observation_id,
        type=o.type,
        name=o.name,
        start_time=o.start_time,
        end_time=o.end_time,
        completion_start_time=o.completion_start_time,
        input=_loads(o.input),
        output=_loads(o.output),
        metadata=dict(o.metadata_ or {}),
        level=o.level,
        status_message=o.status_message,
        version=o.version,
        environment=o.environment,
        model=o.model,
        model_parameters=dict(o.model_parameters or {}),
        usage_details=dict(o.usage_details or {}),
        cost_details=dict(o.cost_details or {}),
        cost_source=o.cost_source,
        usage_source=o.usage_source,
        prompt_name=o.prompt_name,
        prompt_version=o.prompt_version,
        event_ts=o.event_ts,
    )


def score_to_row(s: Score) -> ScoreRow:
    return ScoreRow(
        id=s.id,
        name=s.name,
        timestamp=s.timestamp,
        trace_id=s.trace_id,
        observation_id=s.observation_id,
        session_id=s.session_id,
        dataset_run_id=s.dataset_run_id,
        value=s.value,
        string_value=s.string_value,
        data_type=s.data_type,
        comment=s.comment,
        source=s.source,
        config_id=s.config_id,
        author_id=s.author_id,
        environment=s.environment,
        event_ts=s.event_ts,
    )


def _scores_by_trace(
    session: Session, project_id: str, trace_ids: Sequence[str]
) -> dict[str, list[ScoreRow]]:
    grouped: dict[str, list[ScoreRow]] = defaultdict(list)
    if not trace_ids:
        return grouped
    stmt = (
        select(Score)
        .where(Score.project_id == project_id, Score.trace_id.in_(trace_ids))
        .order_by(Score.timestamp, Score.id)
    )
    for score in session.scalars(stmt):
        grouped[score.trace_id].append(score_to_row(score))
    return grouped


class SqlReadMixin:
    """list_traces and get_trace. Mixed into SqlTraceStore, which supplies `session_factory`."""

    session_factory: Any

    def list_traces(self, project_id: str, query: TraceQuery) -> Page[TraceRow]:
        limit = max(1, min(query.limit, MAX_PAGE_SIZE))
        sort_col = _sort_column(query.sort)
        where = trace_filter_clauses(project_id, query)
        if query.cursor:
            where.append(_keyset(sort_col, query.descending, query.cursor))
        order = sort_col.desc() if query.descending else sort_col.asc()
        stmt = select(Trace).where(*where).order_by(order, Trace.id.asc()).limit(limit + 1)
        with self.session_factory() as session:
            rows = list(session.scalars(stmt))
            more = len(rows) > limit
            rows = rows[:limit]
            items = [trace_to_row(t) for t in rows]
            scores = _scores_by_trace(session, project_id, [t.id for t in rows])
        for item in items:
            item.scores = scores.get(item.id, [])
        next_cursor = None
        if more:
            last = rows[-1]
            sort_value = last.timestamp if query.sort == "timestamp" else _sort_value(last, query)
            next_cursor = encode_cursor(sort_value, last.id)
        return Page(items=items, next_cursor=next_cursor)

    def get_trace(self, project_id: str, trace_id: str) -> TraceDetail | None:
        with self.session_factory() as session:
            trace = session.scalar(
                select(Trace).where(Trace.project_id == project_id, Trace.id == trace_id)
            )
            if trace is None:
                return None
            observations = session.scalars(
                select(Observation)
                .where(Observation.project_id == project_id, Observation.trace_id == trace_id)
                .order_by(Observation.start_time, Observation.id)
            )
            obs_rows = [observation_to_row(o) for o in observations]
            scores = _scores_by_trace(session, project_id, [trace_id]).get(trace_id, [])
            row = trace_to_row(trace)
        row.scores = scores
        return TraceDetail(trace=row, observations=obs_rows, scores=scores)


def _sort_value(trace: Trace, query: TraceQuery) -> float | int:
    if query.sort == "latency":
        return trace.latency_ms if trace.latency_ms is not None else -1.0
    if query.sort == "cost":
        return float(trace.total_cost or 0.0)
    return int(trace.total_tokens or 0)


__all__ = [
    "SqlReadMixin",
    "trace_filter_clauses",
    "trace_to_row",
]
