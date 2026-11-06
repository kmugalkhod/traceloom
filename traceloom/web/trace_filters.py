"""Trace list filters as URL query parameters <-> TraceQuery, and a readable summary."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from traceloom.ports import LEVELS, TraceQuery
from traceloom.web.ranges import environment, resolve_range

TRUE = {"1", "true", "on", "yes"}
FALSE = {"0", "false", "off", "no"}


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _bool(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    return True if lowered in TRUE else False if lowered in FALSE else None


def _time(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return parsed if parsed is None or parsed.tzinfo else parsed.astimezone()


def parse_trace_query(
    params: Mapping[str, str], multi: Mapping[str, list[str]] | None = None
) -> TraceQuery:
    """Filters from a query string. `tags` may repeat or be comma separated; `metadata.<key>`
    carries a metadata equality; `range` and `env` apply when no explicit times/environment."""
    multi = multi or {k: [v] for k, v in params.items()}
    tags = [t.strip() for raw in multi.get("tags", []) for t in raw.split(",") if t.strip()]
    metadata = {
        k[len("metadata.") :]: v
        for k, v in params.items()
        if k.startswith("metadata.") and len(k) > 9
    }
    level = params.get("min_level") or None
    start, end = _time(params.get("from_time")), _time(params.get("to_time"))
    if start is None and end is None and "range" in params:
        _, start, end = resolve_range(params)
    return TraceQuery(
        q=params.get("q") or None,
        name=params.get("name") or None,
        user_id=params.get("user_id") or None,
        session_id=params.get("session_id") or None,
        tags=tuple(tags),
        min_level=level if level in LEVELS else None,
        environment=environment(params),
        release=params.get("release") or None,
        version=params.get("version") or None,
        has_error=_bool(params.get("has_error")),
        metadata=metadata,
        latency_min_ms=_float(params.get("latency_min_ms")),
        latency_max_ms=_float(params.get("latency_max_ms")),
        cost_min=_float(params.get("cost_min")),
        cost_max=_float(params.get("cost_max")),
        bookmarked=_bool(params.get("bookmarked")),
        from_time=start,
        to_time=end,
    )


def describe(query: TraceQuery, params: Mapping[str, str] | None = None) -> list[str]:
    """Short chips for the filters in force, e.g. `level >= ERROR`, `env: staging`."""
    chips = []
    if query.q:
        chips.append(f'search: "{query.q}"')
    for label, value in (
        ("name", query.name),
        ("user", query.user_id),
        ("session", query.session_id),
        ("env", query.environment),
        ("release", query.release),
        ("version", query.version),
    ):
        if value:
            chips.append(f"{label}: {value}")
    if query.tags:
        chips.append("tags: " + ", ".join(query.tags))
    if query.min_level:
        chips.append(f"level ≥ {query.min_level}")
    if query.has_error is not None:
        chips.append("has error" if query.has_error else "no errors")
    chips.extend(f"metadata.{k} = {v}" for k, v in query.metadata.items())
    if query.latency_min_ms is not None or query.latency_max_ms is not None:
        chips.append(f"latency {query.latency_min_ms or 0:g}–{query.latency_max_ms or '∞'} ms")
    if query.cost_min is not None or query.cost_max is not None:
        chips.append(f"cost {query.cost_min or 0:g}–{query.cost_max or '∞'}")
    if query.bookmarked is not None:
        chips.append("bookmarked" if query.bookmarked else "not bookmarked")
    if params and params.get("range") and not (params.get("from_time") or params.get("to_time")):
        chips.append(f"last {params['range']}")
    elif query.from_time or query.to_time:
        start = f"{query.from_time:%Y-%m-%d %H:%M}" if query.from_time else "..."
        stop = f"{query.to_time:%Y-%m-%d %H:%M}" if query.to_time else "now"
        chips.append(f"{start} to {stop}")
    return chips


def to_jsonable(query: TraceQuery) -> dict:
    """The filter as plain JSON for an audit row (only fields in force)."""
    out: dict = {}
    for key, value in vars(query).items():
        if key in ("sort", "descending", "cursor", "limit"):
            continue
        if value in (None, (), {}, ""):
            continue
        out[key] = (
            value.isoformat()
            if isinstance(value, datetime)
            else (list(value) if isinstance(value, tuple) else value)
        )
    return out
