"""AC-11: every dashboard figure equals a direct computation over the raw seeded rows."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

import pytest

from tests.seeding import T0, AnalyticsStore, build_dataset, seed
from traceloom.db.models import Observation, Trace
from traceloom.db.session import make_engine, make_session_factory, upgrade_to_head
from traceloom.ports import DashboardQuery

FROM, TO = T0 + timedelta(days=1), T0 + timedelta(days=4)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    engine = make_engine(f"sqlite:///{tmp_path_factory.mktemp('dash') / 'd.db'}")
    upgrade_to_head(engine)
    sessions = make_session_factory(engine)
    mine, other = build_dataset("p1", 600, 4, seed=3), build_dataset("p2", 300, 3, seed=9)
    seed(sessions, mine)
    seed(sessions, other)
    yield AnalyticsStore(sessions), mine
    engine.dispose()


def pct(values: list[float], p: int) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1]


def ms(delta: timedelta) -> float:
    return delta.total_seconds() * 1000


def raw(data, env, lo=FROM, hi=TO):
    traces = [
        t
        for t in data.traces
        if lo <= t["timestamp"] <= hi and (env is None or t["environment"] == env)
    ]
    obs = [
        o
        for o in data.observations
        if lo <= o["start_time"] <= hi and (env is None or o["environment"] == env)
    ]
    return traces, obs


@pytest.mark.parametrize("env", [None, "production", "staging"])
def test_numbers_match_raw(world, env):
    store, data = world
    got = store.dashboard("p1", DashboardQuery(FROM, TO, environment=env, bucket="hour"))
    traces, obs = raw(data, env)

    assert got.trace_count == len(traces)
    assert got.total_cost == pytest.approx(sum(t["total_cost"] for t in traces))

    cost, tokens = defaultdict(float), defaultdict(int)
    for o in obs:
        if o["model"]:
            cost[o["model"]] += o["cost_details"].get("total", 0)
            tokens[o["model"]] += o["usage_details"].get("total", 0)
    assert {r["model"]: r["cost"] for r in got.cost_by_model} == pytest.approx(dict(cost))
    assert {r["model"]: r["tokens"] for r in got.tokens_by_model} == dict(tokens)
    assert [r["cost"] for r in got.cost_by_model] == pytest.approx(
        sorted(cost.values(), reverse=True)
    )

    by_name = defaultdict(list)
    for t in traces:
        by_name[t["name"]].append(t["latency_ms"])
    assert {r["name"] for r in got.latency_by_trace_name} == set(by_name)
    for row in got.latency_by_trace_name:
        values = by_name[row["name"]]
        assert row["count"] == len(values)
        for p in (50, 90, 95, 99):
            assert row[f"p{p}"] == pytest.approx(pct(values, p))

    by_model = defaultdict(list)
    for o in obs:
        if o["model"] and o["end_time"]:
            by_model[o["model"]].append(ms(o["end_time"] - o["start_time"]))
    assert {r["model"] for r in got.latency_by_model} == set(by_model)
    for row in got.latency_by_model:
        for p in (50, 90, 95, 99):
            assert row[f"p{p}"] == pytest.approx(pct(by_model[row["model"]], p), abs=1e-6)

    levels = Counter(o["level"] for o in obs)
    assert {r["level"]: r["count"] for r in got.level_share} == dict(levels)
    for r in got.level_share:
        assert r["share"] == pytest.approx(levels[r["level"]] / len(obs))
    assert sum(r["share"] for r in got.level_share) == pytest.approx(1.0)

    user_cost = defaultdict(float)
    for t in traces:
        if t["user_id"]:
            user_cost[t["user_id"]] += t["total_cost"]
    top = sorted(user_cost.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    assert [r["user_id"] for r in got.top_users_by_cost] == [u for u, _ in top]
    assert [r["cost"] for r in got.top_users_by_cost] == pytest.approx([c for _, c in top])
    assert len(got.top_users_by_cost) == 10

    hours = Counter(t["timestamp"].replace(minute=0, second=0, microsecond=0) for t in traces)
    assert sum(b["count"] for b in got.traces_over_time) == len(traces)
    assert {b["bucket"]: b["count"] for b in got.traces_over_time if b["count"]} == dict(hours)
    buckets = [b["bucket"] for b in got.traces_over_time]
    assert buckets == sorted(buckets) and len(buckets) == len(set(buckets))
    assert buckets[0] == FROM.replace(minute=0, second=0, microsecond=0)


def test_day_buckets(world):
    store, data = world
    got = store.dashboard("p1", DashboardQuery(FROM, TO, bucket="day"))
    traces, _ = raw(data, None)
    days = Counter(
        t["timestamp"].replace(hour=0, minute=0, second=0, microsecond=0) for t in traces
    )
    assert {b["bucket"]: b["count"] for b in got.traces_over_time if b["count"]} == dict(days)
    assert len(got.traces_over_time) == 4


def test_dashboard_is_project_scoped(world):
    store, data = world
    mine = store.dashboard("p1", DashboardQuery(T0, T0 + timedelta(days=6)))
    theirs = store.dashboard("p2", DashboardQuery(T0, T0 + timedelta(days=6)))
    assert mine.trace_count == len(data.traces) == 600 and theirs.trace_count == 300
    assert store.dashboard("nope", DashboardQuery(T0, T0 + timedelta(days=6))).trace_count == 0
    assert {u["user_id"] for u in mine.top_users_by_cost} <= {t["user_id"] for t in data.traces}


def test_empty_range_gives_empty_dashboard(world):
    store, _ = world
    got = store.dashboard("p1", DashboardQuery(T0 - timedelta(days=30), T0 - timedelta(days=29)))
    assert (got.trace_count, got.total_cost, got.cost_by_model, got.level_share) == (0, 0.0, [], [])
    assert all(b["count"] == 0 for b in got.traces_over_time)


def test_tokens_fall_back_to_input_plus_output_without_total(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'f.db'}")
    upgrade_to_head(engine)
    sessions = make_session_factory(engine)
    when = datetime(2026, 11, 2, tzinfo=UTC)
    with sessions.begin() as s:
        s.add(Trace(project_id="p", id="t", timestamp=when))
        s.add(
            Observation(
                project_id="p",
                id="o",
                trace_id="t",
                start_time=when,
                model="m",
                usage_details={"input": 10, "output": 5},
                cost_details={"input": 1.0},
            )
        )
    got = AnalyticsStore(sessions).dashboard(
        "p", DashboardQuery(when - timedelta(hours=1), when + timedelta(hours=1))
    )
    assert got.tokens_by_model == [{"model": "m", "tokens": 15}]
    assert got.cost_by_model == [{"model": "m", "cost": 0.0}]  # cost needs a `total` key
    engine.dispose()
