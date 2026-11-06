"""The traces list's URL filters -> TraceQuery, for delete-by-filter and its readable summary.

Parsing is the list page's own (`trace_list.parse_state`), so "Delete matching" removes exactly
the traces the list shows for the same URL. `tests/api/test_filter_parity.py` pins that.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

from starlette.datastructures import QueryParams

from traceloom.ports import TraceQuery
from traceloom.web import trace_list

DEFAULT_RANGE = trace_list.DEFAULT_RANGE
RANGES = trace_list.RANGES
# Parameters that only affect paging, sorting or display, never which traces match.
NON_FILTER = {"cursor", "shown", "sort"}


def parse_trace_query(
    params: QueryParams, env_pref: str | None = None, now: datetime | None = None
) -> TraceQuery:
    """Exactly the query the traces list runs for this URL (its default range, saved
    environment and tab handling included), minus paging and sort."""
    return trace_list.parse_state(params, "", env_pref, now=now).query


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
    """Short chips for the filters in force, worded as on the list."""
    state = trace_list.parse_state(params or QueryParams(""), "", query.environment)
    chips = [chip.label for chip in state.chips]
    if state.query.q:
        chips.insert(0, f'search: "{state.query.q}"')
    if state.environment:
        chips.insert(0, f"env: {state.environment}")
    chips.append(state.range_phrase.replace("in the ", "").replace("in ", ""))
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
