"""trace_stats and list_environments: lite always, ClickHouse under TRACELOOM_IT=1."""

from __future__ import annotations

import pytest

from tests.contract.stores import minutes
from traceloom.ports import TraceQuery

P = "proj-stats"


def seed_set(seed):
    seed(
        P,
        "a",
        timestamp=minutes(1),
        total_cost=0.5,
        latency_ms=100.0,
        max_level="DEFAULT",
        environment="production",
    )
    seed(
        P,
        "b",
        timestamp=minutes(2),
        total_cost=1.5,
        latency_ms=200.0,
        max_level="ERROR",
        environment="production",
    )
    seed(
        P,
        "c",
        timestamp=minutes(3),
        total_cost=2.0,
        latency_ms=300.0,
        max_level="WARNING",
        environment="staging",
    )
    seed(
        P,
        "d",
        timestamp=minutes(4),
        total_cost=0.0,
        latency_ms=None,
        max_level="DEFAULT",
        environment="staging",
    )
    seed(
        "other",
        "z",
        timestamp=minutes(1),
        total_cost=99.0,
        latency_ms=5.0,
        max_level="ERROR",
        environment="elsewhere",
    )


def test_stats_over_everything(read_store, seed):
    seed_set(seed)
    stats = read_store.trace_stats(P, TraceQuery())
    assert (stats.count, stats.error_count) == (4, 1)
    assert stats.total_cost == pytest.approx(4.0)
    assert stats.latency_p50_ms == pytest.approx(200.0)  # null latencies are ignored
    assert stats.latency_p95_ms == pytest.approx(290.0)


def test_stats_follow_the_filters_but_not_sort_or_paging(read_store, seed):
    seed_set(seed)
    prod = read_store.trace_stats(
        P, TraceQuery(environment="production", limit=1, sort="cost", cursor=None)
    )
    assert (prod.count, prod.error_count) == (2, 1) and prod.total_cost == pytest.approx(2.0)
    assert read_store.trace_stats(P, TraceQuery(min_level="WARNING")).count == 2
    assert read_store.trace_stats(P, TraceQuery(from_time=minutes(3))).count == 2
    assert read_store.trace_stats(P, TraceQuery(bookmarked=True)).count == 0


def test_stats_of_nothing(read_store, seed):
    stats = read_store.trace_stats("nobody", TraceQuery())
    assert (stats.count, stats.total_cost, stats.error_count) == (0, 0.0, 0)
    assert stats.latency_p50_ms is None and stats.latency_p95_ms is None


def test_stats_are_project_scoped(read_store, seed):
    seed_set(seed)
    assert read_store.trace_stats("other", TraceQuery()).total_cost == pytest.approx(99.0)
    assert read_store.trace_stats(P, TraceQuery()).total_cost == pytest.approx(4.0)


def test_environments_are_distinct_sorted_and_scoped(read_store, seed):
    seed_set(seed)
    assert read_store.list_environments(P) == ["production", "staging"]
    assert read_store.list_environments("other") == ["elsewhere"]
    assert read_store.list_environments("nobody") == []
