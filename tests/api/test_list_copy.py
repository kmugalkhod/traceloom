"""The traces list KPI copy: sub-cent totals stay visible and counts agree with their noun."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from traceloom.auth.permissions import Role
from traceloom.ports import ObservationRow, TraceRow

NOW = datetime.now(UTC)


def add(container, project_id, trace_id, *, cost=None, level="DEFAULT"):
    ts = NOW - timedelta(minutes=5)
    obs = ObservationRow(
        id=f"{trace_id}-o",
        trace_id=trace_id,
        start_time=ts,
        end_time=ts + timedelta(seconds=1),
        event_ts=ts,
        level=level,
        cost_details={"total": cost} if cost is not None else None,
    )
    trace = TraceRow(id=trace_id, timestamp=ts, event_ts=ts, name=trace_id)
    container.trace_store.upsert(project_id, [trace], [obs], [])
    container.trace_store.refresh_trace_aggregates(project_id, [trace_id])


def stat(html, label):
    block = re.search(rf'<div class="top">{label}.*?<b>([^<]+)</b><span class="sub">([^<]+)', html)
    assert block, f"no {label} stat"
    return block.group(1), block.group(2)


def page(client, login, world):
    login(client, world.user(Role.VIEWER))
    return client.get(f"/project/{world.project.id}/traces", headers={"Accept": "text/html"}).text


def test_sub_cent_total_cost_is_not_shown_as_zero(client, login, world, container):
    add(container, world.project.id, "t-cheap", cost=0.0009)
    value, _ = stat(page(client, login, world), "Total cost")
    assert value == "$0.0009"


def test_one_error_trace_reads_singular(client, login, world, container):
    add(container, world.project.id, "t-err", level="ERROR")
    add(container, world.project.id, "t-ok")
    _, sub = stat(page(client, login, world), "Error rate")
    assert sub == "1 trace with an Error"


def test_several_error_traces_read_plural(client, login, world, container):
    add(container, world.project.id, "t-err-1", level="ERROR")
    add(container, world.project.id, "t-err-2", level="ERROR")
    _, sub = stat(page(client, login, world), "Error rate")
    assert sub == "2 traces with an Error"
