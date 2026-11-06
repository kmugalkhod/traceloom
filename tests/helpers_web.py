"""Helpers for page-level tests: seed traces through the store, read CSRF tokens off pages."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from traceloom.ports import ObservationRow, ScoreRow, TraceRow

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def seed_trace(
    container,
    project_id: str,
    trace_id: str,
    *,
    observations: int = 2,
    cost: float | None = None,
    tokens: int | None = None,
    level: str | None = None,
    duration_ms: float | None = None,
    scores: list[ScoreRow] | None = None,
    **fields: Any,
) -> TraceRow:
    """Write a trace with N observations (first is the root) through the real store."""
    ts = fields.pop("timestamp", T0)
    trace = TraceRow(id=trace_id, timestamp=ts, name=fields.pop("name", trace_id), **fields)
    obs = [
        ObservationRow(
            id=f"{trace_id}-o{i}",
            trace_id=trace_id,
            start_time=ts + timedelta(milliseconds=10 * i),
            end_time=ts + timedelta(milliseconds=10 * i + 8),
            parent_observation_id=None if i == 0 else f"{trace_id}-o0",
            name="root" if i == 0 else f"step{i}",
            type="span",
            event_ts=ts,
        )
        for i in range(observations)
    ]
    if level is not None:
        obs[0].level = level
    if duration_ms is not None:
        obs[0].end_time = ts + timedelta(milliseconds=duration_ms)
    if cost is not None:
        obs[0].cost_details = {"total": cost}
    if tokens is not None:
        obs[0].usage_details = {"total": tokens}
    trace.event_ts = ts
    container.trace_store.upsert(project_id, [trace], obs, scores or [])
    container.trace_store.refresh_trace_aggregates(project_id, [trace_id])
    return trace


def form_token(html: str, action: str) -> str:
    """The csrf_token a rendered form carries, found by the form's action."""
    pattern = (
        r'<form[^>]*action="' + re.escape(action) + r'"[^>]*>\s*'
        r'<input type="hidden" name="csrf_token" value="([^"]*)"'
    )
    match = re.search(pattern, html)
    assert match, f"no form with action {action!r} in the page"
    token = match.group(1)
    assert token, f"form {action!r} rendered an empty csrf token"
    return token


def post_form(client, html: str, action: str, data: dict[str, str], **kwargs):
    """POST like a browser: the token comes from the rendered form, not a header."""
    return client.post(
        action,
        data={"csrf_token": form_token(html, action), **data},
        follow_redirects=False,
        **kwargs,
    )


def score(trace_id: str, name: str, value: float, **over: Any) -> ScoreRow:
    return ScoreRow(
        id=over.pop("id", f"{trace_id}-{name}"),
        name=name,
        timestamp=T0,
        trace_id=trace_id,
        value=value,
        **over,
    )
