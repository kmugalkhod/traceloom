"""The traces list's URL filters -> TraceQuery, for delete-by-filter and its readable summary.

This mirrors `traceloom.web.trace_list.parse_state` (the list page) parameter for parameter,
including the 24 hour default range and the saved environment, so "Delete matching" removes
exactly the traces the list shows. `tests/api/test_filter_parity.py` compares the two as soon as
both are on the same branch; after the merge this module can call `parse_state` directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from starlette.datastructures import QueryParams

from traceloom.ports import LEVELS, TraceQuery

RANGES = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
DEFAULT_RANGE = "24h"
MAX_METADATA_FILTERS = 10
# Parameters that only affect paging, sorting or display, never which traces match.
NON_FILTER = {"cursor", "shown", "sort"}


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _day(value: str | None, end: bool = False) -> datetime | None:
    try:
        parsed = datetime.strptime(value or "", "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    return parsed + timedelta(days=1) - timedelta(milliseconds=1) if end else parsed


def parse_trace_query(
    params: QueryParams, env_pref: str | None = None, now: datetime | None = None
) -> TraceQuery:
    now = now or datetime.now(UTC)
    get = params.get
    rng = get("range") or DEFAULT_RANGE
    if rng not in RANGES and rng != "custom":
        rng = DEFAULT_RANGE
    if rng == "custom":
        from_time, to_time = _day(get("from")), _day(get("to"), end=True)
        if from_time is None and to_time is None:
            from_time = now - RANGES[DEFAULT_RANGE]
    else:
        from_time, to_time = now - RANGES[rng], None

    tab = get("tab")
    level = get("level") if get("level") in LEVELS else None
    min_level = level
    has_error = {"1": True, "0": False}.get(get("has_error") or "")
    bookmarked = True if (get("bookmarked") == "1" or tab == "bookmarked") else None
    if tab == "errors":
        has_error = True
    if tab == "warnings" and (
        min_level is None or LEVELS.index(min_level) < LEVELS.index("WARNING")
    ):
        min_level = "WARNING"

    metadata = {}
    for key, value in params.multi_items():
        if (
            key.startswith("metadata.")
            and len(key) > len("metadata.")
            and len(metadata) < MAX_METADATA_FILTERS
        ):
            metadata[key[len("metadata.") :]] = value

    env_param = get("env")
    environment = None if env_param == "all" else (env_param or env_pref or None)
    lat_min, lat_max = _float(get("lat_min")), _float(get("lat_max"))
    return TraceQuery(
        q=(get("q") or "").strip() or None,
        name=get("name") or None,
        user_id=get("user") or None,
        session_id=get("session") or None,
        tags=tuple(t for t in params.getlist("tag") if t),
        min_level=min_level,
        environment=environment,
        release=get("release") or None,
        version=get("version") or None,
        has_error=has_error,
        metadata=metadata,
        latency_min_ms=lat_min * 1000 if lat_min is not None else None,
        latency_max_ms=lat_max * 1000 if lat_max is not None else None,
        cost_min=_float(get("cost_min")),
        cost_max=_float(get("cost_max")),
        bookmarked=bookmarked,
        from_time=from_time,
        to_time=to_time,
    )


def explicit_filter(params: QueryParams, env_pref: str | None) -> str:
    """The filter as a query string that no longer depends on defaults or the saved
    environment: what the delete dialog stores, so the POST matches what the GET counted."""
    pairs = [(k, v) for k, v in params.multi_items() if k not in NON_FILTER]
    keys = {k for k, _ in pairs}
    if "range" not in keys or params.get("range") not in (*RANGES, "custom"):
        pairs = [(k, v) for k, v in pairs if k != "range"] + [("range", DEFAULT_RANGE)]
    env = params.get("env")
    if env is None and env_pref:
        pairs.append(("env", env_pref))
    return urlencode(pairs)


def describe(query: TraceQuery, params: QueryParams | None = None) -> list[str]:
    """Short chips for the filters in force, in the list's wording."""
    p = params or QueryParams("")
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
    chips.extend(f"tag: {t}" for t in query.tags)
    if query.min_level:
        chips.append(f"level ≥ {query.min_level}")
    if query.has_error is not None:
        chips.append("has error" if query.has_error else "no errors")
    if query.bookmarked:
        chips.append("bookmarked")
    chips.extend(f"metadata.{k} = {v}" for k, v in query.metadata.items())
    if query.latency_min_ms is not None or query.latency_max_ms is not None:
        low = f"{query.latency_min_ms / 1000:g}" if query.latency_min_ms is not None else "0"
        high = f"{query.latency_max_ms / 1000:g}" if query.latency_max_ms is not None else "∞"
        chips.append(f"latency {low}–{high} s")
    if query.cost_min is not None or query.cost_max is not None:
        low = f"{query.cost_min:g}" if query.cost_min is not None else "0"
        high = f"{query.cost_max:g}" if query.cost_max is not None else "∞"
        chips.append(f"cost {low}–{high}")
    rng = p.get("range")
    if rng == "custom":
        chips.append(f"{p.get('from') or '...'} to {p.get('to') or 'now'}")
    else:
        labels = {
            "1h": "last hour",
            "24h": "last 24 hours",
            "7d": "last 7 days",
            "30d": "last 30 days",
        }
        chips.append(labels.get(rng or DEFAULT_RANGE, labels[DEFAULT_RANGE]))
    return chips


def to_jsonable(query: TraceQuery) -> dict:
    """The filter as plain JSON for an audit row (only fields in force)."""
    out: dict = {}
    for key, value in vars(query).items():
        if key in ("sort", "descending", "cursor", "limit"):
            continue
        if value in (None, (), {}, ""):
            continue
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif isinstance(value, tuple):
            out[key] = list(value)
        else:
            out[key] = value
    return out
