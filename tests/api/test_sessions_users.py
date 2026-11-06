from __future__ import annotations

import dataclasses
from collections import defaultdict
from datetime import datetime, timedelta

import pytest

from tests.seeding import T0, AnalyticsStore, build_dataset, seed
from traceloom.db.models import Score, Trace
from traceloom.db.session import make_engine, make_session_factory, upgrade_to_head
from traceloom.ports import InvalidCursor, SessionQuery, UserQuery


@pytest.fixture
def sessions(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 's.db'}")
    upgrade_to_head(engine)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def store(sessions):
    seed(sessions, build_dataset("p1", 400, 2, seed=5))
    seed(sessions, build_dataset("p2", 150, 2, seed=8))
    return AnalyticsStore(sessions)


def collect(fetch, project_id, query):
    """Every item across all pages."""
    items, cursor = [], None
    while True:
        page = fetch(project_id, dataclasses.replace(query, cursor=cursor))
        items += page.items
        cursor = page.next_cursor
        if cursor is None:
            return items


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def raw_sessions(traces):
    groups = defaultdict(list)
    for t in traces:
        if t["session_id"]:
            groups[t["session_id"]].append(t)
    out = {}
    for sid, ts in groups.items():
        first, last = min(t["timestamp"] for t in ts), max(t["timestamp"] for t in ts)
        out[sid] = dict(
            first=first,
            last=last,
            n=len(ts),
            cost=sum(t["total_cost"] for t in ts),
            duration=max(ms(t["timestamp"]) + t["latency_ms"] for t in ts)
            - min(ms(t["timestamp"]) for t in ts),
            users={t["user_id"] for t in ts if t["user_id"]},
            tags={g for t in ts for g in t["tags"]},
        )
    return out


# --- sessions ---


def test_session_replay_order_and_total(sessions):
    base = T0 + timedelta(hours=1)
    order = [3, 0, 4, 1, 2]  # ingested out of order
    rows = []
    for k in order:
        rows.append(
            Trace(
                project_id="p",
                id=f"t{k}",
                session_id="conv",
                user_id=f"u{k % 2}",
                timestamp=base + timedelta(minutes=k),
                latency_ms=1500.0,
                total_cost=0.1 * (k + 1),
                tags=["x"] if k == 0 else ["y"],
                input=f'"q{k}"',
                output=f'"a{k}"',
            )
        )
    rows.append(Trace(project_id="p", id="other", session_id="conv2", timestamp=base))
    rows.append(
        Trace(project_id="q", id="leak", session_id="conv", timestamp=base, total_cost=99.0)
    )
    with sessions.begin() as s:
        s.add_all(rows)
        s.add(Score(project_id="p", id="sc", trace_id="t2", name="good", value=1.0, timestamp=base))
    detail = AnalyticsStore(sessions).get_session("p", "conv")
    assert [t.id for t in detail.traces] == ["t0", "t1", "t2", "t3", "t4"]
    assert [t.input for t in detail.traces] == ["q0", "q1", "q2", "q3", "q4"]
    assert detail.session.total_cost == pytest.approx(sum(t.total_cost for t in detail.traces))
    assert detail.session.total_cost == pytest.approx(1.5)
    assert detail.session.trace_count == 5
    assert detail.session.first_trace_time == base
    assert detail.session.last_trace_time == base + timedelta(minutes=4)
    assert detail.session.duration_ms == 4 * 60_000 + 1500
    assert detail.session.user_ids == ["u0", "u1"] and detail.session.tags == ["x", "y"]
    assert [s.name for s in detail.traces[2].scores] == ["good"]
    assert AnalyticsStore(sessions).get_session("p", "missing") is None
    assert AnalyticsStore(sessions).get_session("q", "conv2") is None  # other project's id


def test_list_sessions_matches_raw(store):
    data = build_dataset("p1", 400, 2, seed=5)
    expected = raw_sessions(data.traces)
    items = collect(store.list_sessions, "p1", SessionQuery(limit=17))
    assert len(items) == len(expected) == len({i.session_id for i in items})
    for s in items:
        e = expected[s.session_id]
        assert (s.first_trace_time, s.last_trace_time, s.trace_count) == (
            e["first"],
            e["last"],
            e["n"],
        )
        assert s.total_cost == pytest.approx(e["cost"])
        assert s.duration_ms == e["duration"]
        assert set(s.user_ids) == e["users"] and set(s.tags) == e["tags"]
    assert [s.last_trace_time for s in items] == sorted(
        (s.last_trace_time for s in items), reverse=True
    )


@pytest.mark.parametrize(
    ("sort", "key", "descending"),
    [
        ("cost", lambda s: s.total_cost, True),
        ("cost", lambda s: s.total_cost, False),
        ("traces", lambda s: s.trace_count, True),
        ("duration", lambda s: s.duration_ms, False),
        ("first_trace", lambda s: s.first_trace_time, False),
    ],
)
def test_session_sorts_paginate_without_gaps_or_repeats(store, sort, key, descending):
    pages = collect(
        store.list_sessions, "p1", SessionQuery(sort=sort, descending=descending, limit=11)
    )
    ids = [s.session_id for s in pages]
    assert (
        len(ids) == len(set(ids)) == len(raw_sessions(build_dataset("p1", 400, 2, seed=5).traces))
    )
    values = [key(s) for s in pages]
    assert values == sorted(values, reverse=descending)


def test_session_filters_and_scoping(store):
    data = build_dataset("p1", 400, 2, seed=5)
    lo, hi = T0 + timedelta(days=1), T0 + timedelta(days=3)
    for env in ("production", "staging"):
        in_scope = [
            t for t in data.traces if t["environment"] == env and lo <= t["timestamp"] <= hi
        ]
        expected = raw_sessions(in_scope)
        got = store.list_sessions(
            "p1", SessionQuery(environment=env, from_time=lo, to_time=hi, limit=200)
        ).items
        assert {s.session_id: s.trace_count for s in got} == {
            k: v["n"] for k, v in expected.items()
        }
    assert store.list_sessions("nobody", SessionQuery()).items == []
    other = raw_sessions(build_dataset("p2", 150, 2, seed=8).traces)
    shared = sorted(set(other) & set(raw_sessions(data.traces)))
    assert shared  # the same session ids exist in both projects
    for sid in shared:
        mine, theirs = store.get_session("p1", sid), store.get_session("p2", sid)
        assert mine.session.trace_count == raw_sessions(data.traces)[sid]["n"]
        assert theirs.session.trace_count == other[sid]["n"]
        assert theirs.session.total_cost == pytest.approx(other[sid]["cost"])


def test_session_cursor_errors(store):
    page = store.list_sessions("p1", SessionQuery(sort="cost", limit=2))
    with pytest.raises(InvalidCursor):
        store.list_sessions("p1", SessionQuery(sort="last_trace", cursor=page.next_cursor))
    with pytest.raises(InvalidCursor):
        store.list_sessions("p1", SessionQuery(cursor="garbage"))
    with pytest.raises(ValueError):
        store.list_sessions("p1", SessionQuery(sort="nope"))


# --- users ---


def raw_users(traces):
    groups = defaultdict(list)
    for t in traces:
        if t["user_id"]:
            groups[t["user_id"]].append(t)
    return groups


def test_list_users_matches_raw(store):
    data = build_dataset("p1", 400, 2, seed=5)
    expected = raw_users(data.traces)
    items = collect(store.list_users, "p1", UserQuery(sort="cost", limit=9))
    assert {u.user_id for u in items} == set(expected) and len(items) == len(expected)
    for u in items:
        ts = expected[u.user_id]
        assert u.trace_count == len(ts)
        assert u.first_seen == min(t["timestamp"] for t in ts) and u.last_seen == max(
            t["timestamp"] for t in ts
        )
        assert u.total_tokens == sum(t["total_tokens"] for t in ts)
        assert u.total_cost == pytest.approx(sum(t["total_cost"] for t in ts))
    assert [u.total_cost for u in items] == sorted((u.total_cost for u in items), reverse=True)


def test_list_users_env_filter_and_scoping(store):
    data = build_dataset("p1", 400, 2, seed=5)
    staging = raw_users([t for t in data.traces if t["environment"] == "staging"])
    got = store.list_users("p1", UserQuery(environment="staging", limit=200)).items
    assert {u.user_id: u.trace_count for u in got} == {k: len(v) for k, v in staging.items()}
    assert store.list_users("nobody", UserQuery()).items == []
    with pytest.raises(ValueError):
        store.list_users("p1", UserQuery(sort="nope"))


def test_user_summary(store, sessions):
    data = build_dataset("p1", 400, 2, seed=5)
    expected = raw_users(data.traces)
    uid = max(expected, key=lambda u: len(expected[u]))
    ts = expected[uid]
    summary = store.get_user_summary("p1", uid)
    assert summary.user.trace_count == len(ts)
    daily = defaultdict(lambda: [0, 0, 0.0])
    for t in ts:
        d = daily[t["timestamp"].date()]
        d[0] += 1
        d[1] += t["total_tokens"]
        d[2] += t["total_cost"]
    assert [d["day"] for d in summary.daily] == sorted(daily)
    for d in summary.daily:
        assert (d["traces"], d["tokens"]) == tuple(daily[d["day"]][:2])
        assert d["cost"] == pytest.approx(daily[d["day"]][2])
    assert sum(d["traces"] for d in summary.daily) == len(ts)
    recent = sorted(ts, key=lambda t: (-ms(t["timestamp"]), t["id"]))[:50]
    assert [t.id for t in summary.recent_traces] == [t["id"] for t in recent]
    assert len(summary.recent_traces) == min(50, len(ts))
    their_sessions = {t["session_id"] for t in ts if t["session_id"]}
    assert {s.session_id for s in summary.recent_sessions} == their_sessions
    for s in summary.recent_sessions:  # only this user's traces count
        assert s.trace_count == sum(1 for t in ts if t["session_id"] == s.session_id)
    ids = {t["id"] for t in ts}
    raw_scores = [sc for sc in data.scores if sc["trace_id"] in ids]
    assert {s.id for s in summary.recent_scores} == {sc["id"] for sc in raw_scores}
    assert store.get_user_summary("p1", "nobody") is None
    assert store.get_user_summary("p2-only", uid) is None


def test_recent_lists_are_capped_at_50(sessions):
    base = T0
    with sessions.begin() as s:
        for i in range(80):
            s.add(
                Trace(
                    project_id="p",
                    id=f"t{i}",
                    user_id="u",
                    session_id=f"s{i}",
                    timestamp=base + timedelta(minutes=i),
                )
            )
            s.add(
                Score(
                    project_id="p",
                    id=f"sc{i}",
                    trace_id=f"t{i}",
                    name="n",
                    value=1.0,
                    timestamp=base + timedelta(minutes=i),
                )
            )
    summary = AnalyticsStore(sessions).get_user_summary("p", "u")
    assert [
        len(summary.recent_traces),
        len(summary.recent_sessions),
        len(summary.recent_scores),
    ] == [50, 50, 50]
    assert summary.recent_traces[0].id == "t79" and summary.user.trace_count == 80
