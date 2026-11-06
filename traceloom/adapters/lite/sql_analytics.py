"""Sessions, users and dashboard aggregates for the lite SQL TraceStore.

The host class (SqlTraceStore) provides `session_factory`. Every statement starts with a
`project_id = :pid` predicate, and `environment` / date filters apply to all of them.

Conventions shared with the ClickHouse adapter: token and cost totals come from the `total` key
of `usage_details` / `cost_details` (falling back to input + output tokens, or the sum of the
cost keys, when a producer left `total` out). Percentiles use linear interpolation, through the
`percentile` aggregate registered on SQLite connections (percentile_cont on Postgres).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Integer, Select, and_, cast, func, or_, select
from sqlalchemy.orm import Session

from traceloom.adapters.lite.sql_read import _scores_by_trace, score_to_row, trace_to_row
from traceloom.db.models import Observation, Score, Trace
from traceloom.ports import (
    LEVELS,
    DashboardData,
    DashboardQuery,
    InvalidCursor,
    Page,
    SessionDetail,
    SessionQuery,
    SessionRow,
    UserQuery,
    UserRow,
    UserSummary,
    decode_cursor,
    encode_cursor,
)

MAX_PAGE_SIZE = 200
RECENT_LIMIT = 50
BREAKDOWN_LIMIT = 50
TOP_USERS = 10
PERCENTILES = (("p50", 0.5), ("p90", 0.9), ("p95", 0.95), ("p99", 0.99))


# --- dialect helpers ---------------------------------------------------------------------


def _dialect(session: Session) -> str:
    return session.get_bind().dialect.name


def _epoch_ms(column: ColumnElement[Any], dialect: str) -> ColumnElement[Any]:
    """Milliseconds since the epoch of a UTC timestamp column (exact to the millisecond)."""
    if dialect == "sqlite":
        seconds = cast(func.strftime("%s", column), Integer)
        millis = cast(func.substr(func.strftime("%f", column), 4, 3), Integer)
        return seconds * 1000 + millis
    return func.extract("epoch", column) * 1000  # postgres (not exercised by the test suite)


def _percentile(column: ColumnElement[Any], p: float, dialect: str) -> ColumnElement[Any]:
    if dialect == "sqlite":
        return func.percentile(column, p)
    return func.percentile_cont(p).within_group(column)  # postgres (untested here)


def _bucket(column: ColumnElement[Any], unit: str, dialect: str) -> ColumnElement[Any]:
    """Start of the hour/day as 'YYYY-MM-DDTHH:MM:SS' text (UTC)."""
    hour = unit == "hour"
    if dialect == "sqlite":
        return func.strftime("%Y-%m-%dT%H:00:00" if hour else "%Y-%m-%dT00:00:00", column)
    fmt = 'YYYY-MM-DD"T"HH24:00:00' if hour else 'YYYY-MM-DD"T"00:00:00'  # postgres (untested)
    return func.to_char(func.date_trunc(unit, column), fmt)


def _day(column: ColumnElement[Any], dialect: str) -> ColumnElement[Any]:
    if dialect == "sqlite":
        return func.strftime("%Y-%m-%d", column)
    return func.to_char(func.date_trunc("day", column), "YYYY-MM-DD")


def _json_number(column: ColumnElement[Any], key: str) -> ColumnElement[Any]:
    return column[key].as_float()


def _obs_tokens() -> ColumnElement[Any]:
    usage = Observation.usage_details
    return func.coalesce(
        _json_number(usage, "total"),
        func.coalesce(_json_number(usage, "input"), 0)
        + func.coalesce(_json_number(usage, "output"), 0),
    )


def _obs_cost() -> ColumnElement[Any]:
    return func.coalesce(_json_number(Observation.cost_details, "total"), 0.0)


def _trace_filters(
    project_id: str, environment: str | None, from_time: datetime | None, to_time: datetime | None
) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = [Trace.project_id == project_id]
    if environment is not None:
        clauses.append(Trace.environment == environment)
    if from_time is not None:
        clauses.append(Trace.timestamp >= from_time)
    if to_time is not None:
        clauses.append(Trace.timestamp <= to_time)
    return clauses


def _page_limit(limit: int) -> int:
    return max(1, min(limit, MAX_PAGE_SIZE))


def _as_ms(value: Any) -> float:
    return float(value or 0)


def _keyset(
    sort_col: ColumnElement[Any],
    id_col: ColumnElement[Any],
    descending: bool,
    cursor: str,
    temporal: bool,
) -> ColumnElement[bool]:
    value, row_id = decode_cursor(cursor)
    if temporal != isinstance(value, datetime) or (
        not temporal and (isinstance(value, bool) or not isinstance(value, int | float))
    ):
        raise InvalidCursor("cursor does not match the sort")
    beyond = sort_col < value if descending else sort_col > value
    return or_(beyond, and_(sort_col == value, id_col > row_id))


def _paged(rows: list, limit: int, cursor_of) -> tuple[list, str | None]:
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, (encode_cursor(*cursor_of(rows[-1])) if more else None)


# --- sessions / users grouping -----------------------------------------------------------


def _group_select(
    key: ColumnElement[Any], dialect: str, where: list[ColumnElement[bool]]
) -> Select:
    """One row per `key` (session or user) over the matching traces."""
    start = _epoch_ms(Trace.timestamp, dialect)
    return (
        select(
            key.label("key"),
            func.min(Trace.timestamp).label("first_ts"),
            func.max(Trace.timestamp).label("last_ts"),
            func.count().label("traces"),
            func.coalesce(func.sum(Trace.total_cost), 0.0).label("cost"),
            func.coalesce(func.sum(Trace.total_tokens), 0).label("tokens"),
            (func.max(start + func.coalesce(Trace.latency_ms, 0)) - func.min(start)).label(
                "duration"
            ),
        )
        .where(*where, key.is_not(None))
        .group_by(key)
    )


SESSION_SORTS = {
    "first_trace": ("first_ts", True),
    "last_trace": ("last_ts", True),
    "traces": ("traces", False),
    "cost": ("cost", False),
    "duration": ("duration", False),
}
USER_SORTS = {
    "first_seen": ("first_ts", True),
    "last_seen": ("last_ts", True),
    "traces": ("traces", False),
    "tokens": ("tokens", False),
    "cost": ("cost", False),
}


def _grouped_page(
    session: Session,
    grouped: Select,
    sorts: dict,
    sort: str,
    descending: bool,
    cursor: str | None,
    limit: int,
) -> tuple[list[Any], str | None]:
    if sort not in sorts:
        raise ValueError(f"unknown sort {sort!r}; expected one of {sorted(sorts)}")
    column, temporal = sorts[sort]
    sub = grouped.subquery()
    sort_col = sub.c[column]
    stmt = select(sub)
    if cursor:
        stmt = stmt.where(_keyset(sort_col, sub.c.key, descending, cursor, temporal))
    order = sort_col.desc() if descending else sort_col.asc()
    rows = list(session.execute(stmt.order_by(order, sub.c.key.asc()).limit(limit + 1)))
    return _paged(rows, limit, lambda r: (getattr(r, column), r.key))


def _session_rows(
    rows: Sequence[Any], members: dict[str, tuple[list[str], list[str]]]
) -> list[SessionRow]:
    return [
        SessionRow(
            session_id=r.key,
            first_trace_time=r.first_ts,
            last_trace_time=r.last_ts,
            trace_count=int(r.traces),
            duration_ms=_as_ms(r.duration),
            total_cost=float(r.cost),
            user_ids=members.get(r.key, ([], []))[0],
            tags=members.get(r.key, ([], []))[1],
        )
        for r in rows
    ]


def _members(
    session: Session, where: list[ColumnElement[bool]], session_ids: Sequence[str]
) -> dict[str, tuple[list[str], list[str]]]:
    """Distinct users and tags per session, in first-seen order."""
    users: dict[str, dict[str, None]] = defaultdict(dict)
    tags: dict[str, dict[str, None]] = defaultdict(dict)
    if session_ids:
        query = (
            select(Trace.session_id, Trace.user_id, Trace.tags)
            .where(*where, Trace.session_id.in_(session_ids))
            .order_by(Trace.timestamp, Trace.id)
        )
        for sid, uid, trace_tags in session.execute(query):
            if uid:
                users[sid][uid] = None
            for tag in trace_tags or []:
                tags[sid][tag] = None
    return {sid: (list(users[sid]), list(tags[sid])) for sid in session_ids}


def _user_row(r: Any) -> UserRow:
    return UserRow(
        user_id=r.key,
        first_seen=r.first_ts,
        last_seen=r.last_ts,
        trace_count=int(r.traces),
        total_tokens=int(r.tokens),
        total_cost=float(r.cost),
    )


def _fill_buckets(
    counts: dict[datetime, int], start: datetime, end: datetime, unit: str
) -> list[dict[str, Any]]:
    step = timedelta(hours=1) if unit == "hour" else timedelta(days=1)
    first = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if unit == "day":
        first = first.replace(hour=0)
    out, cursor = [], first
    while cursor <= end:
        out.append({"bucket": cursor, "count": counts.get(cursor, 0)})
        cursor += step
    return out


class SqlAnalyticsMixin:
    """Sessions, users and the dashboard. Mixed into SqlTraceStore (`session_factory`)."""

    session_factory: Any

    # --- sessions ---------------------------------------------------------------------------

    def list_sessions(self, project_id: str, query: SessionQuery) -> Page[SessionRow]:
        limit = _page_limit(query.limit)
        where = _trace_filters(project_id, query.environment, query.from_time, query.to_time)
        with self.session_factory() as s:
            grouped = _group_select(Trace.session_id, _dialect(s), where)
            rows, next_cursor = _grouped_page(
                s, grouped, SESSION_SORTS, query.sort, query.descending, query.cursor, limit
            )
            members = _members(s, where, [r.key for r in rows])
        return Page(items=_session_rows(rows, members), next_cursor=next_cursor)

    def get_session(self, project_id: str, session_id: str) -> SessionDetail | None:
        where = [Trace.project_id == project_id, Trace.session_id == session_id]
        with self.session_factory() as s:
            grouped = _group_select(Trace.session_id, _dialect(s), where)
            row = s.execute(grouped).first()
            if row is None:
                return None
            members = _members(s, where, [session_id])
            traces = list(
                s.scalars(select(Trace).where(*where).order_by(Trace.timestamp, Trace.id))
            )
            trace_rows = [trace_to_row(t) for t in traces]
            scores = _scores_by_trace(s, project_id, [t.id for t in traces])
        for trace in trace_rows:
            trace.scores = scores.get(trace.id, [])
        return SessionDetail(session=_session_rows([row], members)[0], traces=trace_rows)

    # --- users ------------------------------------------------------------------------------

    def list_users(self, project_id: str, query: UserQuery) -> Page[UserRow]:
        limit = _page_limit(query.limit)
        where = _trace_filters(project_id, query.environment, query.from_time, query.to_time)
        with self.session_factory() as s:
            grouped = _group_select(Trace.user_id, _dialect(s), where)
            rows, next_cursor = _grouped_page(
                s, grouped, USER_SORTS, query.sort, query.descending, query.cursor, limit
            )
        return Page(items=[_user_row(r) for r in rows], next_cursor=next_cursor)

    def get_user_summary(self, project_id: str, user_id: str) -> UserSummary | None:
        where = [Trace.project_id == project_id, Trace.user_id == user_id]
        with self.session_factory() as s:
            dialect = _dialect(s)
            row = s.execute(_group_select(Trace.user_id, dialect, where)).first()
            if row is None:
                return None
            day = _day(Trace.timestamp, dialect).label("day")
            daily = [
                {
                    "day": date.fromisoformat(r.day),
                    "traces": int(r.traces),
                    "tokens": int(r.tokens),
                    "cost": float(r.cost),
                }
                for r in s.execute(
                    select(
                        day,
                        func.count().label("traces"),
                        func.coalesce(func.sum(Trace.total_tokens), 0).label("tokens"),
                        func.coalesce(func.sum(Trace.total_cost), 0.0).label("cost"),
                    )
                    .where(*where)
                    .group_by(day)
                    .order_by(day)
                )
            ]
            recent = list(
                s.scalars(
                    select(Trace)
                    .where(*where)
                    .order_by(Trace.timestamp.desc(), Trace.id)
                    .limit(RECENT_LIMIT)
                )
            )
            traces = [trace_to_row(t) for t in recent]
            sess_where = [*where, Trace.session_id.is_not(None)]
            sessions_sub = _group_select(Trace.session_id, dialect, sess_where).subquery()
            session_rows = list(
                s.execute(
                    select(sessions_sub)
                    .order_by(sessions_sub.c.last_ts.desc(), sessions_sub.c.key)
                    .limit(RECENT_LIMIT)
                )
            )
            members = _members(s, sess_where, [r.key for r in session_rows])
            scores = [
                score_to_row(sc)
                for sc in s.scalars(
                    select(Score)
                    .join(
                        Trace,
                        and_(Trace.project_id == Score.project_id, Trace.id == Score.trace_id),
                    )
                    .where(Score.project_id == project_id, Trace.user_id == user_id)
                    .order_by(Score.timestamp.desc(), Score.id)
                    .limit(RECENT_LIMIT)
                )
            ]
        return UserSummary(
            user=_user_row(row),
            daily=daily,
            recent_traces=traces,
            recent_sessions=_session_rows(session_rows, members),
            recent_scores=scores,
        )

    # --- dashboard --------------------------------------------------------------------------

    def dashboard(self, project_id: str, query: DashboardQuery) -> DashboardData:
        trace_where = _trace_filters(project_id, query.environment, query.from_time, query.to_time)
        obs_where: list[ColumnElement[bool]] = [
            Observation.project_id == project_id,
            Observation.start_time >= query.from_time,
            Observation.start_time <= query.to_time,
        ]
        if query.environment is not None:
            obs_where.append(Observation.environment == query.environment)
        with self.session_factory() as s:
            dialect = _dialect(s)
            count, cost = s.execute(
                select(func.count(), func.coalesce(func.sum(Trace.total_cost), 0.0)).where(
                    *trace_where
                )
            ).one()
            data = DashboardData(trace_count=int(count), total_cost=float(cost))
            data.cost_by_model = self._by_model(s, obs_where, func.sum(_obs_cost()), "cost", float)
            data.tokens_by_model = self._by_model(
                s, obs_where, func.sum(_obs_tokens()), "tokens", int
            )
            data.latency_by_trace_name = self._latency(
                s, dialect, Trace.name, Trace.latency_ms, trace_where, "name"
            )
            duration = _epoch_ms(Observation.end_time, dialect) - _epoch_ms(
                Observation.start_time, dialect
            )
            data.latency_by_model = self._latency(
                s,
                dialect,
                Observation.model,
                duration,
                [*obs_where, Observation.end_time.is_not(None)],
                "model",
            )
            data.level_share = self._level_share(s, obs_where)
            data.top_users_by_cost = [
                {"user_id": r.user_id, "cost": float(r.cost)}
                for r in s.execute(
                    select(Trace.user_id, func.sum(Trace.total_cost).label("cost"))
                    .where(*trace_where, Trace.user_id.is_not(None))
                    .group_by(Trace.user_id)
                    .order_by(func.sum(Trace.total_cost).desc(), Trace.user_id)
                    .limit(TOP_USERS)
                )
            ]
            bucket = _bucket(Trace.timestamp, query.bucket, dialect)
            counts = {
                datetime.fromisoformat(r[0]).replace(tzinfo=UTC): int(r[1])
                for r in s.execute(
                    select(bucket, func.count()).where(*trace_where).group_by(bucket)
                )
            }
        data.traces_over_time = _fill_buckets(counts, query.from_time, query.to_time, query.bucket)
        return data

    @staticmethod
    def _by_model(
        s: Session, where: list, total: ColumnElement[Any], label: str, cast
    ) -> list[dict]:
        value = total.label(label)
        stmt = (
            select(Observation.model, value)
            .where(*where, Observation.model.is_not(None))
            .group_by(Observation.model)
            .order_by(total.desc(), Observation.model)
            .limit(BREAKDOWN_LIMIT)
        )
        return [{"model": m, label: cast(v or 0)} for m, v in s.execute(stmt)]

    @staticmethod
    def _latency(
        s: Session,
        dialect: str,
        group: ColumnElement[Any],
        value: ColumnElement[Any],
        where: list,
        label: str,
    ) -> list[dict[str, Any]]:
        columns = [_percentile(value, p, dialect).label(name) for name, p in PERCENTILES]
        stmt = (
            select(group, func.count().label("count"), *columns)
            .where(*where, group.is_not(None), value.is_not(None))
            .group_by(group)
            .order_by(func.count().desc(), group)
            .limit(BREAKDOWN_LIMIT)
        )
        return [
            {
                label: r[0],
                "count": int(r.count),
                **{n: float(getattr(r, n)) for n, _ in PERCENTILES},
            }
            for r in s.execute(stmt)
        ]

    @staticmethod
    def _level_share(s: Session, where: list) -> list[dict[str, Any]]:
        rows = s.execute(
            select(Observation.level, func.count()).where(*where).group_by(Observation.level)
        )
        counts = {level: n for level, n in rows}
        total = sum(counts.values())
        return [
            {"level": level, "count": int(counts[level]), "share": counts[level] / total}
            for level in LEVELS
            if counts.get(level)
        ]


__all__ = ["SqlAnalyticsMixin"]
