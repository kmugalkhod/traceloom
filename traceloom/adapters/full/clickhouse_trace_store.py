"""ClickHouse TraceStore for full mode.

Query building is separate from execution: every `build_*` function returns `(sql, params)` and
puts `project_id = {project_id:String}` first in its WHERE clause, so the SQL can be unit tested
against a fake client. All reads use FINAL because the tables are ReplacingMergeTree(event_ts).

JSON values are stored as JSON text. Token and cost totals are read from the "total" key of
`usage_details` / `cost_details`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from traceloom.ports import (
    LEVELS,
    DashboardData,
    DashboardQuery,
    InvalidCursor,
    ObservationRow,
    Page,
    PromptGeneration,
    PromptMetrics,
    PromptMetricsQuery,
    ScoreRow,
    SessionDetail,
    SessionQuery,
    SessionRow,
    TraceDetail,
    TraceQuery,
    TraceRow,
    TraceStats,
    UserQuery,
    UserRow,
    UserSummary,
    decode_cursor,
    encode_cursor,
)

SCHEMA_FILE = Path(__file__).with_name("clickhouse_schema.sql")
MAX_PAGE_SIZE = 200
SEARCH_TEXT_LIMIT = 4096
RECENT_LIMIT = 50
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

TRACE_COLUMNS = (
    "project_id id name timestamp input output user_id session_id tags metadata release version "
    "environment public bookmarked event_ts latency_ms total_tokens total_cost max_level "
    "observation_count search_text"
).split()
OBSERVATION_COLUMNS = (
    "project_id id trace_id parent_observation_id type name start_time end_time "
    "completion_start_time input output metadata level status_message version environment model "
    "model_parameters usage_details cost_details cost_source usage_source prompt_name "
    "prompt_version event_ts"
).split()
SCORE_COLUMNS = (
    "project_id id trace_id observation_id session_id dataset_run_id name value string_value "
    "data_type comment source config_id author_id timestamp environment event_ts"
).split()

_TABLES: dict[str, tuple[list[str], type]] = {
    "traces": (TRACE_COLUMNS, TraceRow),
    "observations": (OBSERVATION_COLUMNS, ObservationRow),
    "scores": (SCORE_COLUMNS, ScoreRow),
}
_KIND_TABLE = {"trace": "traces", "observation": "observations", "score": "scores"}
# Columns holding JSON text: (decoded default when NULL/invalid)
_JSON_COLUMNS = {"input", "output", "metadata", "model_parameters", "usage_details", "cost_details"}
_NULLABLE_JSON = {"input", "output"}
_TIME_COLUMNS = {"timestamp", "event_ts", "start_time", "end_time", "completion_start_time"}
# Written by refresh_trace_aggregates and update_trace_flags, never overwritten by an upsert.
_DERIVED_TRACE_FIELDS = {
    "latency_ms",
    "total_tokens",
    "total_cost",
    "max_level",
    "observation_count",
    "search_text",
    "public",
    "bookmarked",
}
_KEY_FIELDS = {"id", "trace_id"}


class ClickHouseError(RuntimeError):
    pass


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _unset(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


# --- Row <-> column conversion ---------------------------------------------------------------


def encode_row(table: str, project_id: str, row: Any) -> list[Any]:
    """Values in column order for an INSERT. JSON columns become JSON text."""
    values = []
    for column in _TABLES[table][0]:
        value = project_id if column == "project_id" else getattr(row, column)
        if column in _JSON_COLUMNS:
            if value is None and column in _NULLABLE_JSON:
                value = None
            else:
                value = json.dumps({} if value is None else value, default=str)
        elif column == "event_ts" and value is None:
            value = _now()
        elif column in _TIME_COLUMNS:
            value = _utc(value)
        values.append(value)
    return values


def decode_row(table: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Column values (as selected) to dataclass kwargs, without project_id."""
    out: dict[str, Any] = {}
    for column in _TABLES[table][0]:
        if column == "project_id":
            continue
        value = record.get(column)
        if column in _JSON_COLUMNS and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass  # leave odd text as it is
        elif column in _JSON_COLUMNS and value is None and column not in _NULLABLE_JSON:
            value = {}
        elif column in _TIME_COLUMNS:
            value = _utc(value)
        elif column == "tags":
            value = list(value or [])
        elif column in ("public", "bookmarked"):
            value = bool(value)
        out[column] = value
    return out


def _to_row(table: str, record: Mapping[str, Any]) -> Any:
    kwargs = decode_row(table, record)
    if table == "traces":
        kwargs["total_tokens"] = int(kwargs.get("total_tokens") or 0)
        kwargs["total_cost"] = float(kwargs.get("total_cost") or 0.0)
        kwargs["search_text"] = kwargs.get("search_text") or ""
    return _TABLES[table][1](**kwargs)


def merge_record(existing: Mapping[str, Any] | None, incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Field-wise merge for an upsert. A newer (or equal) event_ts wins per field, but an unset
    incoming field never erases a stored value; an older event only fills fields that are unset."""
    if existing is None:
        return dict(incoming)
    new_wins = (incoming.get("event_ts") or _now()) >= (existing.get("event_ts") or _now())
    merged = dict(existing)
    for key, value in incoming.items():
        if key in _KEY_FIELDS or key in _DERIVED_TRACE_FIELDS:
            continue
        if _unset(value):
            continue
        if new_wins or _unset(existing.get(key)):
            merged[key] = value
    if new_wins:
        merged["event_ts"] = incoming.get("event_ts") or existing.get("event_ts")
    return merged


# --- SQL building ----------------------------------------------------------------------------


def _project_where(*clauses: str) -> str:
    return " AND ".join(["project_id = {project_id:String}", *clauses])


def _like_pattern(text: str) -> str:
    escaped = text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_SORT_EXPRS = {
    "timestamp": "timestamp",
    "latency": "coalesce(latency_ms, -1)",
    "cost": "total_cost",
    "tokens": "total_tokens",
}


def _cursor_value(sort: str, cursor: str) -> tuple[Any, str]:
    value, row_id = decode_cursor(cursor)
    if sort == "timestamp":
        if not isinstance(value, datetime):
            raise InvalidCursor("cursor does not match the sort")
    elif isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidCursor("cursor does not match the sort")
    return value, row_id


def trace_filters(query: TraceQuery) -> tuple[list[str], dict[str, Any]]:
    """WHERE fragments (without the project predicate) and their parameters."""
    where: list[str] = []
    params: dict[str, Any] = {}
    if query.q:
        params["q"] = _like_pattern(query.q)
        where.append(
            "(lower(search_text) LIKE {q:String} OR lower(coalesce(name, '')) LIKE {q:String}"
            " OR lower(id) LIKE {q:String})"
        )
    for column, value in (
        ("name", query.name),
        ("user_id", query.user_id),
        ("session_id", query.session_id),
        ("environment", query.environment),
        ("release", query.release),
        ("version", query.version),
    ):
        if value is not None:
            where.append(f"{column} = {{{column}:String}}")
            params[column] = value
    if query.tags:
        where.append("hasAny(tags, {tags:Array(String)})")
        params["tags"] = list(query.tags)
    if query.min_level:
        if query.min_level not in LEVELS:
            raise ValueError(f"unknown level {query.min_level!r}")
        where.append("max_level IN {levels:Array(String)}")
        params["levels"] = list(LEVELS[LEVELS.index(query.min_level) :])
    if query.has_error is True:
        where.append("max_level = 'ERROR'")
    elif query.has_error is False:
        where.append("max_level != 'ERROR'")
    for i, (key, value) in enumerate(query.metadata.items()):
        where.append(
            f"if(JSONType(metadata, {{mk{i}:String}}) = 'String',"
            f" JSONExtractString(metadata, {{mk{i}:String}}),"
            f" JSONExtractRaw(metadata, {{mk{i}:String}})) = {{mv{i}:String}}"
        )
        params[f"mk{i}"], params[f"mv{i}"] = key, str(value)
    for column, low, high, name in (
        ("latency_ms", query.latency_min_ms, query.latency_max_ms, "latency"),
        ("total_cost", query.cost_min, query.cost_max, "cost"),
    ):
        if low is not None:
            where.append(f"{column} >= {{{name}_min:Float64}}")
            params[f"{name}_min"] = low
        if high is not None:
            where.append(f"{column} <= {{{name}_max:Float64}}")
            params[f"{name}_max"] = high
    if query.bookmarked is not None:
        where.append("bookmarked = {bookmarked:Bool}")
        params["bookmarked"] = query.bookmarked
    if query.from_time is not None:
        where.append("timestamp >= {from_time:DateTime64(3, 'UTC')}")
        params["from_time"] = _utc(query.from_time)
    if query.to_time is not None:
        where.append("timestamp <= {to_time:DateTime64(3, 'UTC')}")
        params["to_time"] = _utc(query.to_time)
    return where, params


def build_list_traces(
    project_id: str, query: TraceQuery, table: str = "traces"
) -> tuple[str, dict[str, Any]]:
    """Keyset-paged trace list. Order is (sort expr, id asc); one extra row detects a next page."""
    try:
        sort_expr = _SORT_EXPRS[query.sort]
    except KeyError:
        raise ValueError(
            f"unknown sort {query.sort!r}; expected one of {sorted(_SORT_EXPRS)}"
        ) from None
    where, params = trace_filters(query)
    params["project_id"] = project_id
    if query.cursor:
        value, row_id = _cursor_value(query.sort, query.cursor)
        op = "<" if query.descending else ">"
        type_ = "DateTime64(3, 'UTC')" if query.sort == "timestamp" else "Float64"
        where.append(
            f"({sort_expr} {op} {{cursor_value:{type_}}}"
            f" OR ({sort_expr} = {{cursor_value:{type_}}} AND id > {{cursor_id:String}}))"
        )
        params["cursor_value"] = _utc(value) if query.sort == "timestamp" else float(value)
        params["cursor_id"] = row_id
    limit = max(1, min(query.limit, MAX_PAGE_SIZE))
    direction = "DESC" if query.descending else "ASC"
    sql = (
        f"SELECT {', '.join(TRACE_COLUMNS)} FROM {table} FINAL WHERE {_project_where(*where)}"
        f" ORDER BY {sort_expr} {direction}, id ASC LIMIT {limit + 1}"
    )
    return sql, params


def build_trace_ids(project_id: str, query: TraceQuery, table: str = "traces"):
    where, params = trace_filters(query)
    params["project_id"] = project_id
    sql = f"SELECT id FROM {table} FINAL WHERE {_project_where(*where)}"
    return sql, params


def build_trace_stats(project_id: str, query: TraceQuery, table: str = "traces"):
    where, params = trace_filters(query)
    params["project_id"] = project_id
    sql = (
        "SELECT count() AS count, sum(total_cost) AS total_cost,"
        " countIf(max_level = 'ERROR') AS error_count,"
        " quantileExact(0.5)(latency_ms) AS p50, quantileExact(0.95)(latency_ms) AS p95"
        f" FROM {table} FINAL WHERE {_project_where(*where)}"
    )
    return sql, params


def build_environments(project_id: str, table: str = "traces"):
    sql = (
        f"SELECT DISTINCT environment FROM {table} FINAL"
        f" WHERE {_project_where()} ORDER BY environment"
    )
    return sql, {"project_id": project_id}


def _by_ids(
    table: str,
    columns: Sequence[str],
    where_col: str,
    ids: Sequence[str],
    project_id: str,
    order: str,
):
    sql = (
        f"SELECT {', '.join(columns)} FROM {table} FINAL"
        f" WHERE {_project_where(f'{where_col} IN {{ids:Array(String)}}')} ORDER BY {order}"
    )
    return sql, {"project_id": project_id, "ids": list(ids)}


def build_get_trace(project_id: str, trace_id: str, table: str = "traces"):
    sql = (
        f"SELECT {', '.join(TRACE_COLUMNS)} FROM {table} FINAL"
        f" WHERE {_project_where('id = {id:String}')} LIMIT 1"
    )
    return sql, {"project_id": project_id, "id": trace_id}


def build_observations(project_id: str, trace_ids: Sequence[str], table: str = "observations"):
    return _by_ids(table, OBSERVATION_COLUMNS, "trace_id", trace_ids, project_id, "start_time, id")


def build_scores(project_id: str, trace_ids: Sequence[str], table: str = "scores"):
    return _by_ids(table, SCORE_COLUMNS, "trace_id", trace_ids, project_id, "timestamp, id")


def build_existing(project_id: str, kind: str, ids: Sequence[str], database: str = ""):
    table = _KIND_TABLE[kind]
    return _by_ids(
        f"{database}.{table}" if database else table, _TABLES[table][0], "id", ids, project_id, "id"
    )


def build_delete(table: str, project_id: str, column: str, ids: Sequence[str]):
    sql = f"DELETE FROM {table} WHERE {_project_where(f'{column} IN {{ids:Array(String)}}')}"
    return sql, {"project_id": project_id, "ids": list(ids)}


# Sessions and users ---------------------------------------------------------------------------

_SESSION_SORTS = {
    "first_trace": "first_trace_time",
    "last_trace": "last_trace_time",
    "traces": "trace_count",
    "cost": "cost_sum",
    "duration": "duration_ms",
}
_USER_SORTS = {
    "last_seen": "last_seen",
    "first_seen": "first_seen",
    "traces": "trace_count",
    "tokens": "token_sum",
    "cost": "cost_sum",
}
_SESSION_SELECT = (
    "session_id, min(timestamp) AS first_trace_time, max(timestamp) AS last_trace_time,"
    " count() AS trace_count,"
    " dateDiff('millisecond', min(timestamp),"
    " max(addMilliseconds(timestamp, toInt64(coalesce(latency_ms, 0))))) AS duration_ms,"
    " sum(total_cost) AS cost_sum, groupUniqArrayIf(user_id, user_id IS NOT NULL) AS user_ids,"
    " arrayDistinct(groupArrayArray(tags)) AS tag_list"
)
_USER_SELECT = (
    "user_id, min(timestamp) AS first_seen, max(timestamp) AS last_seen, count() AS trace_count,"
    " sum(total_tokens) AS token_sum, sum(total_cost) AS cost_sum"
)


def _time_env(query: SessionQuery | UserQuery, params: dict[str, Any]) -> list[str]:
    where = []
    if query.environment is not None:
        where.append("environment = {environment:String}")
        params["environment"] = query.environment
    if query.from_time is not None:
        where.append("timestamp >= {from_time:DateTime64(3, 'UTC')}")
        params["from_time"] = _utc(query.from_time)
    if query.to_time is not None:
        where.append("timestamp <= {to_time:DateTime64(3, 'UTC')}")
        params["to_time"] = _utc(query.to_time)
    return where


def _grouped_page(select, group_col, sorts, query, where, params, table):
    try:
        sort_expr = sorts[query.sort]
    except KeyError:
        raise ValueError(f"unknown sort {query.sort!r}; expected one of {sorted(sorts)}") from None
    having = ""
    if query.cursor:
        value, row_id = decode_cursor(query.cursor)
        is_time = sort_expr.endswith(("_time", "_seen"))
        if is_time != isinstance(value, datetime):
            raise InvalidCursor("cursor does not match the sort")
        type_ = "DateTime64(3, 'UTC')" if is_time else "Float64"
        op = "<" if query.descending else ">"
        having = (
            f" HAVING {sort_expr} {op} {{cursor_value:{type_}}}"
            f" OR ({sort_expr} = {{cursor_value:{type_}}} AND {group_col} > {{cursor_id:String}})"
        )
        params["cursor_value"] = _utc(value) if is_time else float(value)
        params["cursor_id"] = row_id
    limit = max(1, min(query.limit, MAX_PAGE_SIZE))
    direction = "DESC" if query.descending else "ASC"
    sql = (
        f"SELECT {select} FROM {table} FINAL WHERE {_project_where(*where)}"
        f" GROUP BY {group_col}{having} ORDER BY {sort_expr} {direction}, {group_col} ASC"
        f" LIMIT {limit + 1}"
    )
    return sql, params


def build_list_sessions(project_id: str, query: SessionQuery, table: str = "traces"):
    params: dict[str, Any] = {"project_id": project_id}
    where = ["session_id IS NOT NULL", "session_id != ''", *_time_env(query, params)]
    return _grouped_page(_SESSION_SELECT, "session_id", _SESSION_SORTS, query, where, params, table)


def build_get_session(project_id: str, session_id: str, table: str = "traces"):
    sql = (
        f"SELECT {_SESSION_SELECT} FROM {table} FINAL"
        f" WHERE {_project_where('session_id = {session_id:String}')} GROUP BY session_id"
    )
    return sql, {"project_id": project_id, "session_id": session_id}


def build_session_traces(project_id: str, session_id: str, table: str = "traces"):
    sql = (
        f"SELECT {', '.join(TRACE_COLUMNS)} FROM {table} FINAL"
        f" WHERE {_project_where('session_id = {session_id:String}')} ORDER BY timestamp, id"
    )
    return sql, {"project_id": project_id, "session_id": session_id}


def build_list_users(project_id: str, query: UserQuery, table: str = "traces"):
    params: dict[str, Any] = {"project_id": project_id}
    where = ["user_id IS NOT NULL", "user_id != ''", *_time_env(query, params)]
    return _grouped_page(_USER_SELECT, "user_id", _USER_SORTS, query, where, params, table)


def build_get_user(project_id: str, user_id: str, table: str = "traces"):
    sql = (
        f"SELECT {_USER_SELECT} FROM {table} FINAL"
        f" WHERE {_project_where('user_id = {user_id:String}')} GROUP BY user_id"
    )
    return sql, {"project_id": project_id, "user_id": user_id}


def build_user_daily(project_id: str, user_id: str, table: str = "traces"):
    sql = (
        "SELECT toDate(timestamp) AS day, count() AS traces, sum(total_tokens) AS tokens,"
        f" sum(total_cost) AS cost FROM {table} FINAL"
        f" WHERE {_project_where('user_id = {user_id:String}')} GROUP BY day ORDER BY day"
    )
    return sql, {"project_id": project_id, "user_id": user_id}


def build_user_recent_traces(project_id: str, user_id: str, table: str = "traces"):
    sql = (
        f"SELECT {', '.join(TRACE_COLUMNS)} FROM {table} FINAL"
        f" WHERE {_project_where('user_id = {user_id:String}')}"
        f" ORDER BY timestamp DESC, id ASC LIMIT {RECENT_LIMIT}"
    )
    return sql, {"project_id": project_id, "user_id": user_id}


_HAS_SESSION = "session_id IS NOT NULL AND session_id != ''"


def build_user_recent_sessions(project_id: str, user_id: str, table: str = "traces"):
    sql = (
        f"SELECT {_SESSION_SELECT} FROM {table} FINAL"
        f" WHERE {_project_where('user_id = {user_id:String}', _HAS_SESSION)}"
        f" GROUP BY session_id ORDER BY last_trace_time DESC, session_id ASC LIMIT {RECENT_LIMIT}"
    )
    return sql, {"project_id": project_id, "user_id": user_id}


def build_user_recent_scores(project_id: str, user_id: str, traces: str, scores: str):
    user_traces = (
        f"SELECT id FROM {traces} FINAL"
        " WHERE project_id = {project_id:String} AND user_id = {user_id:String}"
    )
    sql = (
        f"SELECT {', '.join(SCORE_COLUMNS)} FROM {scores} FINAL"
        f" WHERE {_project_where(f'trace_id IN ({user_traces})')}"
        f" ORDER BY timestamp DESC, id ASC LIMIT {RECENT_LIMIT}"
    )
    return sql, {"project_id": project_id, "user_id": user_id}


# Dashboard ------------------------------------------------------------------------------------

_QUANTILES = "quantilesExact(0.5, 0.9, 0.95, 0.99)"
_DURATION_MS = "dateDiff('millisecond', start_time, end_time)"


def _dash_range(query: DashboardQuery, time_col: str) -> tuple[list[str], dict[str, Any]]:
    where = [
        f"{time_col} >= {{from_time:DateTime64(3, 'UTC')}}",
        f"{time_col} <= {{to_time:DateTime64(3, 'UTC')}}",
    ]
    params: dict[str, Any] = {"from_time": _utc(query.from_time), "to_time": _utc(query.to_time)}
    if query.environment is not None:
        where.append("environment = {environment:String}")
        params["environment"] = query.environment
    return where, params


def build_dashboard(
    project_id: str, query: DashboardQuery, database: str = ""
) -> dict[str, tuple[str, dict]]:
    """One SQL statement per dashboard panel, keyed by panel name."""
    t = f"{database}.traces" if database else "traces"
    o = f"{database}.observations" if database else "observations"
    if query.bucket not in ("hour", "day"):
        raise ValueError(f"unknown bucket {query.bucket!r}; expected hour or day")
    tw, params = _dash_range(query, "timestamp")
    ow, _ = _dash_range(query, "start_time")
    params = {**params, "project_id": project_id}
    traces_where, obs_where = _project_where(*tw), _project_where(*ow, "model IS NOT NULL")
    obs_all = _project_where(*ow)
    model_total = "JSONExtractFloat(cost_details, 'total')"
    model_tokens = "JSONExtractInt(usage_details, 'total')"

    def panel(select: str, table: str, where: str, tail: str = "") -> str:
        return f"SELECT {select} FROM {table} FINAL WHERE {where} {tail}".strip()

    panels = {
        "totals": panel("count() AS trace_count, sum(total_cost) AS cost_sum", t, traces_where),
        "cost_by_model": panel(
            f"model, sum({model_total}) AS cost",
            o,
            obs_where,
            "GROUP BY model ORDER BY cost DESC, model",
        ),
        "tokens_by_model": panel(
            f"model, sum({model_tokens}) AS tokens",
            o,
            obs_where,
            "GROUP BY model ORDER BY tokens DESC, model",
        ),
        "latency_by_trace_name": panel(
            f"coalesce(name, '') AS trace_name, {_QUANTILES}(latency_ms) AS q",
            t,
            f"{traces_where} AND latency_ms IS NOT NULL",
            "GROUP BY trace_name ORDER BY trace_name",
        ),
        "latency_by_model": panel(
            f"model, {_QUANTILES}({_DURATION_MS}) AS q",
            o,
            f"{obs_where} AND end_time IS NOT NULL",
            "GROUP BY model ORDER BY model",
        ),
        "level_share": panel("level, count() AS n", o, obs_all, "GROUP BY level ORDER BY level"),
        "top_users_by_cost": panel(
            "user_id, sum(total_cost) AS cost",
            t,
            f"{traces_where} AND user_id IS NOT NULL AND user_id != ''",
            "GROUP BY user_id ORDER BY cost DESC, user_id LIMIT 10",
        ),
        "traces_over_time": panel(
            f"toStartOfInterval(timestamp, INTERVAL 1 {query.bucket}) AS bucket, count() AS n",
            t,
            traces_where,
            "GROUP BY bucket ORDER BY bucket",
        ),
    }
    return {name: (sql, dict(params)) for name, sql in panels.items()}


_OBS_TOKENS = (
    "if(JSONHas(usage_details, 'total'), JSONExtractFloat(usage_details, 'total'),"
    " JSONExtractFloat(usage_details, 'input') + JSONExtractFloat(usage_details, 'output'))"
)
_OBS_COST = "JSONExtractFloat(cost_details, 'total')"


def _prompt_where(
    project_id: str, name: str, version: int | None, query: PromptMetricsQuery | None
) -> tuple[str, dict[str, Any]]:
    clauses = ["type = 'generation'", "prompt_name = {prompt_name:String}"]
    params: dict[str, Any] = {"project_id": project_id, "prompt_name": name}
    if version is not None:
        clauses.append("prompt_version = {prompt_version:Int32}")
        params["prompt_version"] = version
    if query is not None:
        if query.environment is not None:
            clauses.append("environment = {environment:String}")
            params["environment"] = query.environment
        if query.from_time is not None:
            clauses.append("start_time >= {from_time:DateTime64(3, 'UTC')}")
            params["from_time"] = _utc(query.from_time)
        if query.to_time is not None:
            clauses.append("start_time <= {to_time:DateTime64(3, 'UTC')}")
            params["to_time"] = _utc(query.to_time)
    return _project_where(*clauses), params


def build_prompt_metrics(
    project_id: str,
    name: str,
    version: int | None,
    query: PromptMetricsQuery | None,
    observations: str = "observations",
    scores: str = "scores",
) -> dict[str, tuple[str, dict]]:
    """The totals statement and the per-score-name averages, keyed by panel name."""
    where, params = _prompt_where(project_id, name, version, query)
    totals = (
        f"SELECT count() AS count, sum({_OBS_TOKENS}) AS tokens, sum({_OBS_COST}) AS cost,"
        f" quantileExactIf(0.5)({_DURATION_MS}, end_time IS NOT NULL) AS median"
        f" FROM {observations} FINAL WHERE {where}"
    )
    numeric = _project_where("data_type = {numeric:String}", "value IS NOT NULL")
    averages = (
        "SELECT name, avg(value) AS average"
        f" FROM {scores} FINAL WHERE {numeric}"
        f" AND trace_id IN (SELECT trace_id FROM {observations} FINAL WHERE {where})"
        " GROUP BY name ORDER BY name"
    )
    return {"totals": (totals, params), "scores": (averages, {**params, "numeric": "NUMERIC"})}


def build_prompt_generations(
    project_id: str,
    name: str,
    version: int | None,
    limit: int,
    query: PromptMetricsQuery | None,
    observations: str = "observations",
):
    where, params = _prompt_where(project_id, name, version, query)
    sql = (
        f"SELECT id, trace_id, prompt_version, start_time, model, level,"
        f" if(end_time IS NULL, NULL, {_DURATION_MS}) AS latency_ms,"
        f" {_OBS_TOKENS} AS tokens, {_OBS_COST} AS cost"
        f" FROM {observations} FINAL WHERE {where}"
        " ORDER BY start_time DESC, id LIMIT {limit:UInt32}"
    )
    return sql, {**params, "limit": max(1, min(limit, MAX_PAGE_SIZE))}


# --- Aggregates ------------------------------------------------------------------------------


def compute_aggregates(trace: TraceRow, observations: Sequence[ObservationRow]) -> TraceRow:
    """Latency, tokens, cost, max level, count and search text derived from observations."""
    starts = [o.start_time for o in observations]
    ends = [o.end_time or o.start_time for o in observations]
    level_rank = {name: i for i, name in enumerate(LEVELS)}
    trace.observation_count = len(observations)
    trace.latency_ms = (max(ends) - min(starts)).total_seconds() * 1000.0 if observations else None
    trace.total_tokens = sum(int((o.usage_details or {}).get("total", 0)) for o in observations)
    trace.total_cost = float(
        sum(float((o.cost_details or {}).get("total", 0)) for o in observations)
    )
    trace.max_level = max(
        (o.level for o in observations), key=lambda x: level_rank.get(x, 0), default="DEFAULT"
    )
    parts = [trace.name, _text(trace.input), _text(trace.output), *(o.name for o in observations)]
    trace.search_text = " ".join(p for p in parts if p)[:SEARCH_TEXT_LIMIT]
    return trace


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, default=str)


# --- Store -----------------------------------------------------------------------------------


def split_statements(sql: str) -> list[str]:
    """Statements of the schema file: split on a semicolon ending a line, comments dropped."""
    lines = [ln for ln in sql.splitlines() if not ln.lstrip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";\n") if s.strip().rstrip(";")]


def apply_schema(client: Any, database: str) -> None:
    if not _IDENT.match(database):
        raise ValueError(f"invalid database name {database!r}")
    client.command(f"CREATE DATABASE IF NOT EXISTS {database}")
    for statement in split_statements(SCHEMA_FILE.read_text()):
        statement = statement.rstrip(";")
        client.command(statement.replace("EXISTS ", f"EXISTS {database}.", 1))


class ClickHouseTraceStore:
    """TraceStore over a clickhouse_connect client (anything with query/insert/command)."""

    def __init__(self, client: Any, database: str = "traceloom") -> None:
        if not _IDENT.match(database):
            raise ValueError(f"invalid database name {database!r}")
        self.client = client
        self.database = database
        self.traces = f"{database}.traces"
        self.observations = f"{database}.observations"
        self.scores = f"{database}.scores"

    # execution helpers
    def _select(self, built: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
        sql, params = built
        result = self.client.query(sql, parameters=params)
        return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]

    def _insert(self, table: str, project_id: str, rows: Sequence[Any]) -> None:
        if rows:
            columns = _TABLES[table][0]
            data = [encode_row(table, project_id, r) for r in rows]
            self.client.insert(f"{self.database}.{table}", data, column_names=columns)

    # writes
    def get_existing(
        self, project_id: str, ids_by_kind: Mapping[str, Sequence[str]]
    ) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for kind, ids in ids_by_kind.items():
            if kind not in _KIND_TABLE:
                raise ValueError(f"unknown kind {kind!r}")
            table = _KIND_TABLE[kind]
            found = {}
            if ids:
                for record in self._select(build_existing(project_id, kind, ids, self.database)):
                    found[record["id"]] = decode_row(table, record)
            out[kind] = found
        return out

    def upsert(
        self,
        project_id: str,
        traces: Sequence[TraceRow],
        observations: Sequence[ObservationRow],
        scores: Sequence[ScoreRow],
    ) -> None:
        existing = self.get_existing(
            project_id,
            {
                "trace": [t.id for t in traces],
                "observation": [o.id for o in observations],
                "score": [s.id for s in scores],
            },
        )
        for table, kind, rows in (
            ("traces", "trace", traces),
            ("observations", "observation", observations),
            ("scores", "score", scores),
        ):
            merged = []
            for row in rows:
                incoming = {c: getattr(row, c) for c in _TABLES[table][0] if c != "project_id"}
                incoming["event_ts"] = incoming["event_ts"] or _now()
                record = merge_record(existing[kind].get(row.id), incoming)
                merged.append(_TABLES[table][1](**{k: record[k] for k in incoming}))
            self._insert(table, project_id, merged)

    def refresh_trace_aggregates(self, project_id: str, trace_ids: Sequence[str]) -> None:
        if not trace_ids:
            return
        trace_rows = self._select(build_trace_rows(project_id, trace_ids, self.traces))
        obs_rows = self._select(build_observations(project_id, trace_ids, self.observations))
        by_trace: dict[str, list[ObservationRow]] = {}
        for record in obs_rows:
            by_trace.setdefault(record["trace_id"], []).append(_to_row("observations", record))
        # Same event_ts as the stored row: with equal versions the later insert wins.
        refreshed = [
            compute_aggregates(_to_row("traces", rec), by_trace.get(rec["id"], []))
            for rec in trace_rows
        ]
        self._insert("traces", project_id, refreshed)

    def update_trace_flags(self, project_id: str, trace_id: str, **fields: Any) -> bool:
        allowed = {"bookmarked", "public", "tags"}
        if not set(fields) <= allowed:
            raise ValueError(f"cannot update {sorted(set(fields) - allowed)}")
        rows = self._select(build_get_trace(project_id, trace_id, self.traces))
        if not rows:
            return False
        trace = _to_row("traces", rows[0])
        for key, value in fields.items():
            setattr(trace, key, list(value) if key == "tags" else bool(value))
        self._insert("traces", project_id, [trace])
        return True

    def add_score(self, project_id: str, score: ScoreRow) -> None:
        self.upsert(project_id, [], [], [score])

    def matching_trace_ids(self, project_id: str, query: TraceQuery) -> list[str]:
        return [r["id"] for r in self._select(build_trace_ids(project_id, query, self.traces))]

    def delete_traces(self, project_id: str, target: Sequence[str] | TraceQuery) -> int:
        if isinstance(target, TraceQuery):
            ids = self.matching_trace_ids(project_id, target)
        else:
            wanted = list(dict.fromkeys(target))
            found = self.get_existing(project_id, {"trace": wanted})["trace"]
            ids = [i for i in wanted if i in found]
        for start in range(0, len(ids), 1000):
            chunk = ids[start : start + 1000]
            for table, column in (
                ("traces", "id"),
                ("observations", "trace_id"),
                ("scores", "trace_id"),
            ):
                sql, params = build_delete(f"{self.database}.{table}", project_id, column, chunk)
                self.client.command(sql, parameters=params)
        return len(ids)

    # reads
    def _attach_scores(self, project_id: str, items: list[TraceRow]) -> None:
        if not items:
            return
        by_trace: dict[str, list[ScoreRow]] = {}
        for rec in self._select(build_scores(project_id, [t.id for t in items], self.scores)):
            by_trace.setdefault(rec["trace_id"], []).append(_to_row("scores", rec))
        for item in items:
            item.scores = by_trace.get(item.id, [])

    def list_traces(self, project_id: str, query: TraceQuery) -> Page[TraceRow]:
        limit = max(1, min(query.limit, MAX_PAGE_SIZE))
        records = self._select(build_list_traces(project_id, query, self.traces))
        more = len(records) > limit
        items = [_to_row("traces", r) for r in records[:limit]]
        self._attach_scores(project_id, items)
        next_cursor = None
        if more and items:
            last = items[-1]
            sort_value = {
                "timestamp": last.timestamp,
                "latency": -1.0 if last.latency_ms is None else last.latency_ms,
                "cost": last.total_cost,
                "tokens": last.total_tokens,
            }[query.sort]
            next_cursor = encode_cursor(sort_value, last.id)
        return Page(items=items, next_cursor=next_cursor)

    def trace_stats(self, project_id: str, query: TraceQuery) -> TraceStats:
        (row,) = self._select(build_trace_stats(project_id, query, self.traces))

        def finite(value: Any) -> float | None:
            return None if value is None or value != value else float(value)  # NaN: no rows

        return TraceStats(
            int(row["count"]),
            float(row["total_cost"] or 0.0),
            int(row["error_count"]),
            finite(row["p50"]) if row["count"] else None,
            finite(row["p95"]) if row["count"] else None,
        )

    def list_environments(self, project_id: str) -> list[str]:
        return [r["environment"] for r in self._select(build_environments(project_id, self.traces))]

    def get_trace(self, project_id: str, trace_id: str) -> TraceDetail | None:
        rows = self._select(build_get_trace(project_id, trace_id, self.traces))
        if not rows:
            return None
        trace = _to_row("traces", rows[0])
        observations = [
            _to_row("observations", r)
            for r in self._select(build_observations(project_id, [trace_id], self.observations))
        ]
        scores = [
            _to_row("scores", r)
            for r in self._select(build_scores(project_id, [trace_id], self.scores))
        ]
        trace.scores = scores
        return TraceDetail(trace=trace, observations=observations, scores=scores)

    def _session(self, record: Mapping[str, Any]) -> SessionRow:
        return SessionRow(
            session_id=record["session_id"],
            first_trace_time=_utc(record["first_trace_time"]),
            last_trace_time=_utc(record["last_trace_time"]),
            trace_count=int(record["trace_count"]),
            duration_ms=float(record["duration_ms"]),
            total_cost=float(record["cost_sum"]),
            user_ids=sorted(record["user_ids"] or []),
            tags=sorted(record["tag_list"] or []),
        )

    def _paged(self, built, query, make, id_key, sort_attrs) -> Page:
        limit = max(1, min(query.limit, MAX_PAGE_SIZE))
        records = self._select(built)
        items = [make(r) for r in records[:limit]]
        next_cursor = None
        if len(records) > limit:
            last = items[-1]
            next_cursor = encode_cursor(
                getattr(last, sort_attrs[query.sort]), getattr(last, id_key)
            )
        return Page(items=items, next_cursor=next_cursor)

    def list_sessions(self, project_id: str, query: SessionQuery) -> Page[SessionRow]:
        built = build_list_sessions(project_id, query, self.traces)
        attrs = {
            "first_trace": "first_trace_time",
            "last_trace": "last_trace_time",
            "traces": "trace_count",
            "cost": "total_cost",
            "duration": "duration_ms",
        }
        return self._paged(built, query, self._session, "session_id", attrs)

    def get_session(self, project_id: str, session_id: str) -> SessionDetail | None:
        rows = self._select(build_get_session(project_id, session_id, self.traces))
        if not rows:
            return None
        traces = [
            _to_row("traces", r)
            for r in self._select(build_session_traces(project_id, session_id, self.traces))
        ]
        self._attach_scores(project_id, traces)
        return SessionDetail(session=self._session(rows[0]), traces=traces)

    @staticmethod
    def _user(record: Mapping[str, Any]) -> UserRow:
        return UserRow(
            user_id=record["user_id"],
            first_seen=_utc(record["first_seen"]),
            last_seen=_utc(record["last_seen"]),
            trace_count=int(record["trace_count"]),
            total_tokens=int(record["token_sum"]),
            total_cost=float(record["cost_sum"]),
        )

    def list_users(self, project_id: str, query: UserQuery) -> Page[UserRow]:
        built = build_list_users(project_id, query, self.traces)
        attrs = {
            "last_seen": "last_seen",
            "first_seen": "first_seen",
            "traces": "trace_count",
            "tokens": "total_tokens",
            "cost": "total_cost",
        }
        return self._paged(built, query, self._user, "user_id", attrs)

    def get_user_summary(self, project_id: str, user_id: str) -> UserSummary | None:
        rows = self._select(build_get_user(project_id, user_id, self.traces))
        if not rows:
            return None
        daily = [
            {
                "day": r["day"],
                "traces": int(r["traces"]),
                "tokens": int(r["tokens"]),
                "cost": float(r["cost"]),
            }
            for r in self._select(build_user_daily(project_id, user_id, self.traces))
        ]
        recent = [
            _to_row("traces", r)
            for r in self._select(build_user_recent_traces(project_id, user_id, self.traces))
        ]
        self._attach_scores(project_id, recent)
        sessions = [
            self._session(r)
            for r in self._select(build_user_recent_sessions(project_id, user_id, self.traces))
        ]
        scores = [
            _to_row("scores", r)
            for r in self._select(
                build_user_recent_scores(project_id, user_id, self.traces, self.scores)
            )
        ]
        return UserSummary(self._user(rows[0]), daily, recent, sessions, scores)

    def dashboard(self, project_id: str, query: DashboardQuery) -> DashboardData:
        panels = {
            name: self._select(built)
            for name, built in build_dashboard(project_id, query, self.database).items()
        }
        totals = panels["totals"][0] if panels["totals"] else {}
        levels = panels["level_share"]
        level_total = sum(int(r["n"]) for r in levels) or 1

        def pct(row: Mapping[str, Any]) -> dict[str, Any]:
            p50, p90, p95, p99 = (float(x) for x in row["q"])
            return {"p50": p50, "p90": p90, "p95": p95, "p99": p99}

        return DashboardData(
            trace_count=int(totals.get("trace_count", 0)),
            total_cost=float(totals.get("cost_sum", 0.0)),
            cost_by_model=[
                {"model": r["model"], "cost": float(r["cost"])} for r in panels["cost_by_model"]
            ],
            tokens_by_model=[
                {"model": r["model"], "tokens": int(r["tokens"])} for r in panels["tokens_by_model"]
            ],
            latency_by_trace_name=[
                {"name": r["trace_name"], **pct(r)} for r in panels["latency_by_trace_name"]
            ],
            latency_by_model=[{"model": r["model"], **pct(r)} for r in panels["latency_by_model"]],
            level_share=[
                {
                    "level": r["level"],
                    "count": int(r["n"]),
                    "share": int(r["n"]) / level_total,
                }
                for r in levels
            ],
            top_users_by_cost=[
                {"user_id": r["user_id"], "cost": float(r["cost"])}
                for r in panels["top_users_by_cost"]
            ],
            traces_over_time=[
                {"bucket": _utc(r["bucket"]), "count": int(r["n"])}
                for r in panels["traces_over_time"]
            ],
        )

    def prompt_metrics(
        self,
        project_id: str,
        name: str,
        version: int | None,
        query: PromptMetricsQuery | None = None,
    ) -> PromptMetrics:
        built = build_prompt_metrics(
            project_id, name, version, query, self.observations, self.scores
        )
        (totals,) = self._select(built["totals"])
        count = int(totals["count"])
        median = totals["median"]
        return PromptMetrics(
            count=count,
            median_latency_ms=float(median) if count and median == median else None,
            total_tokens=int(totals["tokens"] or 0),
            total_cost=float(totals["cost"] or 0.0),
            scores={r["name"]: float(r["average"]) for r in self._select(built["scores"])},
        )

    def list_prompt_generations(
        self,
        project_id: str,
        name: str,
        version: int | None,
        limit: int = RECENT_LIMIT,
        query: PromptMetricsQuery | None = None,
    ) -> list[PromptGeneration]:
        built = build_prompt_generations(project_id, name, version, limit, query, self.observations)
        return [
            PromptGeneration(
                observation_id=r["id"],
                trace_id=r["trace_id"],
                version=r["prompt_version"],
                start_time=_utc(r["start_time"]),
                model=r["model"],
                latency_ms=None if r["latency_ms"] is None else float(r["latency_ms"]),
                tokens=int(r["tokens"] or 0),
                cost=float(r["cost"] or 0.0),
                level=r["level"],
            )
            for r in self._select(built)
        ]


def build_trace_rows(project_id: str, trace_ids: Sequence[str], table: str = "traces"):
    return _by_ids(table, TRACE_COLUMNS, "id", trace_ids, project_id, "id")


__all__ = [
    "ClickHouseTraceStore",
    "apply_schema",
    "build_dashboard",
    "build_list_traces",
    "compute_aggregates",
    "merge_record",
]
