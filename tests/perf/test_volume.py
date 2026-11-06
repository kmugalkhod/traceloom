"""AC-7 / NFR-1: lite mode at 10k traces and 50k observations."""

from __future__ import annotations

import dataclasses
import time
from datetime import timedelta

import pytest

from tests.seeding import T0, AnalyticsStore, build_dataset, seed
from traceloom.db.session import make_engine, make_session_factory, upgrade_to_head
from traceloom.ports import DashboardQuery, SessionQuery, TraceQuery, UserQuery

pytestmark = pytest.mark.perf

LIST_BUDGET, DETAIL_BUDGET, DASHBOARD_BUDGET = 1.0, 0.5, 2.0


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    engine = make_engine(f"sqlite:///{tmp_path_factory.mktemp('perf') / 'perf.db'}")
    upgrade_to_head(engine)
    sessions = make_session_factory(engine)
    seed(sessions, build_dataset("p1", 10_000, 5, seed=11))
    yield AnalyticsStore(sessions)
    engine.dispose()


def timed(fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, time.perf_counter() - start


def test_seed_has_the_expected_volume(store):
    page = store.list_traces("p1", TraceQuery(limit=200))
    assert len(page.items) == 200
    dash = store.dashboard("p1", DashboardQuery(T0, T0 + timedelta(days=6)))
    assert dash.trace_count == 10_000
    assert sum(r["count"] for r in dash.level_share) == 50_000


def test_first_list_page_under_a_second(store):
    page, took = timed(store.list_traces, "p1", TraceQuery())
    assert len(page.items) == 50 and page.next_cursor
    assert took < LIST_BUDGET, f"{took:.3f}s"


def test_filtered_next_page_under_a_second(store):
    query = TraceQuery(environment="production", min_level="WARNING", tags=["a"], sort="cost")
    first, _ = timed(store.list_traces, "p1", query)
    assert first.next_cursor
    second, took = timed(
        store.list_traces, "p1", dataclasses.replace(query, cursor=first.next_cursor)
    )
    assert second.items and took < LIST_BUDGET, f"{took:.3f}s"
    assert all(t.environment == "production" and "a" in t.tags for t in second.items)


def test_text_search_under_a_second(store):
    page, took = timed(store.list_traces, "p1", TraceQuery(q="t99"))
    assert page.items and took < LIST_BUDGET, f"{took:.3f}s"


def test_detail_under_500ms(store):
    detail, took = timed(store.get_trace, "p1", "t4242")
    assert len(detail.observations) == 5 and took < DETAIL_BUDGET, f"{took:.3f}s"


def test_dashboard_under_2s(store):
    full = DashboardQuery(T0, T0 + timedelta(days=6), bucket="hour")
    data, took = timed(store.dashboard, "p1", full)
    assert data.trace_count == 10_000 and took < DASHBOARD_BUDGET, f"{took:.3f}s"
    filtered, took = timed(
        store.dashboard, "p1", DashboardQuery(T0, T0 + timedelta(days=6), environment="staging")
    )
    assert 0 < filtered.trace_count < 10_000 and took < DASHBOARD_BUDGET, f"{took:.3f}s"


def test_sessions_and_users_lists_are_fast_too(store):
    _, took = timed(store.list_sessions, "p1", SessionQuery())
    assert took < LIST_BUDGET, f"sessions {took:.3f}s"
    _, took = timed(store.list_users, "p1", UserQuery(sort="cost"))
    assert took < LIST_BUDGET, f"users {took:.3f}s"
