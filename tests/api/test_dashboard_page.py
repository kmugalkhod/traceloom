"""The dashboard page: every panel, the range and environment filters, empty and safe states."""
# ruff: noqa: E501  (seeding lines are long tables of literals)

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from traceloom.auth.permissions import Role
from traceloom.ports import DashboardQuery, ObservationRow, TraceRow

NOW = datetime.now(UTC)
XSS = "<script>alert(1)</script>"


def pid(world):
    return world.project.id


def add(container, world, tid, *, ago, user=None, name="chat", env="production", model=None,
        tokens=0, cost=0.0, level="DEFAULT", dur=1000, project=None):  # fmt: skip
    start = NOW - ago
    trace = TraceRow(
        id=tid, timestamp=start, name=name, user_id=user, environment=env, event_ts=start
    )
    obs = [
        ObservationRow(
            id=f"{tid}-o", trace_id=tid, start_time=start, end_time=start + timedelta(milliseconds=dur),
            type="generation" if model else "span", model=model, level=level, environment=env,
            usage_details={"input": tokens // 2, "output": tokens - tokens // 2, "total": tokens} if tokens else {},
            cost_details={"total": cost} if cost else {}, event_ts=start,
        )
    ]  # fmt: skip
    container.trace_store.upsert(project or pid(world), [trace], obs, [])
    container.trace_store.refresh_trace_aggregates(project or pid(world), [tid])


@pytest.fixture
def seeded(container, world):
    add(
        container,
        world,
        "a1",
        ago=timedelta(hours=2),
        user="alice",
        model="model-a",
        tokens=1000,
        cost=0.50,
        dur=2000,
    )
    add(
        container,
        world,
        "a2",
        ago=timedelta(hours=3),
        user="alice",
        model="model-a",
        tokens=3000,
        cost=1.50,
        dur=4000,
    )
    add(container, world, "b1", ago=timedelta(hours=4), user="bob", model="model-b", tokens=500, cost=0.25, name="search", level="ERROR", dur=500)  # fmt: skip
    add(container, world, "c1", ago=timedelta(hours=5), user="carol", name="search", dur=300)
    add(
        container,
        world,
        "s1",
        ago=timedelta(hours=1),
        user="alice",
        env="staging",
        model="model-a",
        tokens=200,
        cost=0.10,
    )
    add(container, world, "old", ago=timedelta(days=3), user="dave", model="model-b", tokens=800, cost=0.40)  # fmt: skip


def page(client, login, world, query="", role=Role.VIEWER):
    login(client, world.user(role), csrf=False)
    return client.get(f"/project/{pid(world)}/dashboard{query}")


def numbers(html, label):
    block = re.search(rf'<div class="stat"><div class="top">{label}</div><b>([^<]+)</b>', html)
    return block.group(1)


def expected(container, world, **kw):
    end = datetime.now(UTC)
    return container.trace_store.dashboard(
        pid(world),
        DashboardQuery(from_time=end - kw.pop("span", timedelta(hours=24)), to_time=end, **kw),
    )


def test_every_panel_renders_with_the_stores_numbers(client, login, world, seeded, container):
    r = page(client, login, world)
    assert r.status_code == 200
    data = expected(container, world)
    assert numbers(r.text, "Traces") == f"{data.trace_count:,}" == "5"
    assert numbers(r.text, "Total cost") == f"${data.total_cost:,.4f}"
    assert numbers(r.text, "Total tokens") == "4.70k"
    for title in ("Traces over time", "Error rate by level", "Cost by model", "Tokens by model",
                  "Latency by trace name", "Latency by model", "Top users by cost"):  # fmt: skip
        assert f'aria-label="{title}"' in r.text, title
    cost = {m["model"]: m["cost"] for m in data.cost_by_model}
    assert cost == {"model-a": pytest.approx(2.10), "model-b": pytest.approx(0.25)}
    assert re.search(r"model-a</span>.*?\$2\.1000", r.text, re.S)
    assert re.search(r"model-b</span>.*?\$0\.2500", r.text, re.S)
    assert re.search(r"model-a</span>.*?<span class=\"num\">4,200</span>", r.text, re.S)
    users = re.findall(r'users/(\w+)">', r.text)
    assert users == [u["user_id"] for u in data.top_users_by_cost] == ["alice", "bob", "carol"]
    assert (
        "ERROR" in r.text and "observation at ERROR" in r.text or "observations at ERROR" in r.text
    )


def test_latency_percentiles_per_name_and_model_match_the_store(
    client, login, world, seeded, container
):
    r = page(client, login, world)
    data = expected(container, world)
    by_name = {row["name"]: row for row in data.latency_by_trace_name}
    row = re.search(r'<td class="mono name">search</td>(.*?)</tr>', r.text, re.S).group(1)
    cells = re.findall(r'<td class="num">(?:<span[^>]*><i[^>]*></i>)?([^<]+)', row)
    from traceloom.web.pagefmt import fmt_duration

    assert cells == [fmt_duration(by_name["search"][k]) for k in ("p50", "p90", "p95", "p99")]
    assert 'data-px="' in row  # the bar width travels as a data attribute (no inline style)
    assert any(m["model"] == "model-a" for m in data.latency_by_model)


def test_range_and_environment_change_every_panel(client, login, world, seeded, container):
    day = page(client, login, world)
    week = page(client, login, world, "?range=7d")
    assert numbers(day.text, "Traces") == "5" and numbers(week.text, "Traces") == "6"
    assert "$2.7500" in week.text and "dave" in week.text and "dave" not in day.text
    assert 'aria-pressed="true">7d' in week.text and 'aria-pressed="true">24h' in day.text
    staging = page(client, login, world, "?env=staging")
    assert (
        numbers(staging.text, "Traces") == "1" and numbers(staging.text, "Total cost") == "$0.1000"
    )
    assert "alice" in staging.text and "bob" not in staging.text and "model-b" not in staging.text
    assert "env=staging&amp;range=7d" in page(client, login, world, "?env=staging&range=30d").text


def test_chart_data_matches_buckets(client, login, world, seeded, container):
    day = page(client, login, world)
    match = re.search(r"data-chart='([^']+)'", day.text)
    import html as htmllib
    import json

    spec = json.loads(htmllib.unescape(match.group(1)))
    assert spec["kind"] == "bar" and sum(spec["series"][0]["values"]) == 5
    assert len(spec["labels"]) == len(spec["series"][0]["values"]) >= 24  # hourly buckets
    week = json.loads(
        htmllib.unescape(
            re.search(r"data-chart='([^']+)'", page(client, login, world, "?range=7d").text).group(
                1
            )
        )
    )
    assert sum(week["series"][0]["values"]) == 6 and len(week["labels"]) <= 9  # daily buckets


def test_empty_project_shows_an_empty_state(client, login, world):
    r = page(client, login, world)
    assert r.status_code == 200
    assert "No traces in the last 24 hours" in r.text and "Traceback" not in r.text
    assert 'class="statrow"' not in r.text
    admin = page(client, login, world, role=Role.ADMIN)
    assert "create an API key" in admin.text and "create an API key" not in r.text
    assert "No traces" in page(client, login, world, "?env=nowhere&range=30d").text


def test_unknown_range_falls_back_and_env_is_escaped(client, login, world, seeded):
    r = page(client, login, world, "?range=forever")
    assert r.status_code == 200 and 'aria-pressed="true">24h' in r.text
    r = page(client, login, world, "?env=%3Cscript%3Ealert(1)%3C/script%3E")
    assert "<script>alert" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


def test_model_user_and_trace_names_are_escaped(client, login, world, container):
    add(
        container,
        world,
        "x1",
        ago=timedelta(hours=1),
        user=XSS,
        name=XSS,
        model=XSS,
        tokens=10,
        cost=0.1,
    )
    r = page(client, login, world)
    assert (
        "<script>alert" not in r.text and r.text.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 4
    )
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", r.text)
    assert not re.search(r"\sstyle=", r.text) and not re.search(r"\son\w+=", r.text)


def test_dashboard_is_project_scoped_and_login_protected(
    client, login, world, seeded, make_project, container
):
    other = make_project(world.org, "Other")
    add(
        container,
        world,
        "z1",
        ago=timedelta(hours=1),
        user="zed",
        model="zmodel",
        tokens=1,
        cost=9.0,
        project=other.id,
    )
    r = page(client, login, world)
    assert "zed" not in r.text and "zmodel" not in r.text and "$9.0" not in r.text
    client.cookies.clear()
    assert client.get(f"/project/{pid(world)}/dashboard", follow_redirects=False).status_code in (
        303,
        401,
    )


def test_assets_are_served_for_the_charts(client):
    for path, marker in (
        ("/static/charts.js", "uPlot"),
        ("/static/vendor/uplot.iife.min.js", "uPlot"),
        ("/static/vendor/uplot.min.css", "u-wrap"),
        ("/static/vendor/uplot.LICENSE", "MIT License"),
    ):
        r = client.get(path)
        assert r.status_code == 200 and marker in r.text, path
