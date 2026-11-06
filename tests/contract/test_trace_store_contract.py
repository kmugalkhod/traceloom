"""Write-side contract for TraceStore. Lite always; ClickHouse joins under TRACELOOM_IT=1
once its adapter exists. Read-side cases are added by the read-path task."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from traceloom.adapters.lite.sql_trace_store import SqlTraceStore
from traceloom.db import models
from traceloom.db.session import make_engine, make_session_factory, upgrade_to_head
from traceloom.ports import ObservationRow, ScoreRow, TraceRow

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def sessions(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'c.db'}")
    upgrade_to_head(engine)
    factory = make_session_factory(engine)
    with factory.begin() as s:  # parents of comments (FK enforced)
        s.add_all(
            [
                models.Organization(id="o", name="o"),
                models.User(id="u", email="u@example.com", password_hash="x"),
            ]
        )
        s.flush()
        s.add_all([models.Project(id=p, org_id="o", name=p) for p in ("p1", "p2")])
    yield factory
    engine.dispose()


@pytest.fixture(params=["lite"])
def store(request, sessions):
    return SqlTraceStore(sessions)


def trace(id="t1", ts=0, **kw):
    return TraceRow(id=id, timestamp=at(0), event_ts=at(ts), **kw)


def obs(id="o1", trace_id="t1", ts=0, start=0, end=None, **kw):
    return ObservationRow(
        id=id,
        trace_id=trace_id,
        start_time=at(start),
        end_time=None if end is None else at(end),
        event_ts=at(ts),
        **kw,
    )


def score(id="s1", trace_id="t1", ts=0, **kw):
    return ScoreRow(id=id, trace_id=trace_id, name="q", timestamp=at(0), event_ts=at(ts), **kw)


def test_upsert_twice_leaves_one_row_each(store):
    for _ in range(2):
        store.upsert("p1", [trace()], [obs()], [score()])
    found = store.get_existing("p1", {"trace": ["t1"], "observation": ["o1"], "score": ["s1"]})
    assert [len(found[k]) for k in ("trace", "observation", "score")] == [1, 1, 1]
    assert store.count_traces("p1") == 1


def test_rows_round_trip_all_value_types(store):
    t = trace(
        name="n",
        input={"q": [1, 2]},
        output="text",
        tags=["a"],
        metadata={"k": 1},
        user_id="u1",
        public=True,
    )
    o = obs(
        type="generation",
        model="m",
        usage_details={"input": 3},
        cost_details={"total": 0.5},
        metadata={"x": "y"},
        input=[{"role": "user", "content": "hi"}],
        level="WARNING",
        end=2,
        model_parameters={"temperature": 0.2},
        prompt_version=3,
    )
    sc = score(value=1.0, data_type="BOOLEAN", comment="ok", source="ANNOTATION", author_id="u")
    store.upsert("p1", [t], [o], [sc])
    got = store.get_existing("p1", {"trace": ["t1"], "observation": ["o1"], "score": ["s1"]})
    assert got["trace"]["t1"].input == {"q": [1, 2]} and got["trace"]["t1"].output == "text"
    assert got["trace"]["t1"].tags == ["a"] and got["trace"]["t1"].public is True
    assert got["observation"]["o1"] == o
    assert got["score"]["s1"] == sc


def test_later_event_ts_wins_and_older_is_ignored(store):
    store.upsert("p1", [trace(ts=5, name="new", user_id="u-new")], [], [])
    store.upsert("p1", [trace(ts=1, name="old", user_id="u-old")], [], [])
    row = store.get_existing("p1", {"trace": ["t1"]})["trace"]["t1"]
    assert (row.name, row.user_id) == ("new", "u-new")
    store.upsert("p1", [trace(ts=9, name="newest")], [], [])
    assert store.get_existing("p1", {"trace": ["t1"]})["trace"]["t1"].name == "newest"


def test_equal_event_ts_replaces(store):
    store.upsert("p1", [], [obs(name="a")], [])
    store.upsert("p1", [], [obs(name="b")], [])
    assert store.get_existing("p1", {"observation": ["o1"]})["observation"]["o1"].name == "b"


def test_duplicates_inside_one_call_keep_the_newest(store):
    store.upsert("p1", [trace(ts=2, name="b"), trace(ts=1, name="a")], [], [])
    assert store.get_existing("p1", {"trace": ["t1"]})["trace"]["t1"].name == "b"


def test_aggregates_match_observations(store):
    store.upsert(
        "p1",
        [trace(name="Root", input="Hello World", output={"a": "Bye"})],
        [
            obs(
                "o1",
                start=0,
                end=2,
                usage_details={"input": 10, "output": 5},
                cost_details={"total": 0.25},
                level="DEFAULT",
            ),
            obs(
                "o2",
                start=1,
                end=5,
                usage_details={"total": 7, "input": 1},
                cost_details={"input": 0.1, "output": 0.2},
                level="ERROR",
            ),
            obs("o3", start=3, level="DEBUG"),  # no end: counts as an instant
        ],
        [],
    )
    store.refresh_trace_aggregates("p1", ["t1"])
    row = store.get_existing("p1", {"trace": ["t1"]})["trace"]["t1"]
    assert row.latency_ms == 5000.0
    assert row.total_tokens == 15 + 7
    assert row.total_cost == pytest.approx(0.25 + 0.3)
    assert row.max_level == "ERROR"
    assert row.observation_count == 3
    assert row.search_text == 'root t1 hello world {"a": "bye"}'


def test_refresh_is_idempotent_and_handles_empty_traces(store):
    store.upsert("p1", [trace("empty")], [], [])
    store.refresh_trace_aggregates("p1", ["empty", "empty", "unknown"])
    row = store.get_existing("p1", {"trace": ["empty"]})["trace"]["empty"]
    assert (row.latency_ms, row.total_tokens, row.observation_count) == (None, 0, 0)


def test_search_text_is_truncated_to_4kb(store):
    store.upsert("p1", [trace(input="x" * 10_000)], [], [])
    store.refresh_trace_aggregates("p1", ["t1"])
    assert len(store.get_existing("p1", {"trace": ["t1"]})["trace"]["t1"].search_text) == 4096


def test_upsert_keeps_computed_aggregates(store):
    store.upsert("p1", [trace()], [obs(usage_details={"total": 4})], [])
    store.refresh_trace_aggregates("p1", ["t1"])
    store.upsert("p1", [trace(ts=3, name="renamed")], [], [])  # carries default aggregates
    row = store.get_existing("p1", {"trace": ["t1"]})["trace"]["t1"]
    assert (row.name, row.total_tokens, row.observation_count) == ("renamed", 4, 1)


def test_update_trace_flags(store):
    store.upsert("p1", [trace()], [], [])
    assert store.update_trace_flags("p1", "t1", bookmarked=True, public=True, tags=["a", "a", "b"])
    row = store.get_existing("p1", {"trace": ["t1"]})["trace"]["t1"]
    assert (row.bookmarked, row.public, row.tags) == (True, True, ["a", "b"])
    assert store.update_trace_flags("p1", "missing", bookmarked=True) is False
    with pytest.raises(ValueError):
        store.update_trace_flags("p1", "t1", name="hack")
    with pytest.raises(ValueError):
        store.update_trace_flags("p1", "t1", bookmarked="yes")


def test_add_score(store):
    store.upsert("p1", [trace()], [], [])
    store.add_score("p1", score("s9", value=0.5, source="ANNOTATION"))
    assert store.get_existing("p1", {"score": ["s9"]})["score"]["s9"].source == "ANNOTATION"


def test_delete_removes_children_and_returns_count(store, sessions):
    store.upsert(
        "p1",
        [trace("t1"), trace("t2"), trace("t3")],
        [obs("o1", "t1"), obs("o2", "t2"), obs("o3", "t3")],
        [score("s1", "t1"), score("s2", "t3")],
    )
    with sessions.begin() as s:
        s.add_all(
            [
                models.Comment(
                    project_id="p1",
                    object_type="trace",
                    object_id="t1",
                    trace_id="t1",
                    author_id="u",
                    body="x",
                ),
                models.Comment(
                    project_id="p1",
                    object_type="trace",
                    object_id="t3",
                    trace_id="t3",
                    author_id="u",
                    body="y",
                ),
            ]
        )
    assert store.delete_traces("p1", ["t1", "t2", "missing"]) == 2
    got = store.get_existing(
        "p1",
        {"trace": ["t1", "t2", "t3"], "observation": ["o1", "o2", "o3"], "score": ["s1", "s2"]},
    )
    assert set(got["trace"]) == {"t3"} and set(got["observation"]) == {"o3"}
    assert set(got["score"]) == {"s2"}
    with sessions() as s:
        assert [c.trace_id for c in s.query(models.Comment)] == ["t3"]


def test_projects_are_isolated(store):
    store.upsert("p1", [trace(name="one")], [obs()], [score()])
    store.upsert("p2", [trace(name="two")], [], [])
    assert store.get_existing("p2", {"trace": ["t1"], "observation": ["o1"], "score": ["s1"]}) == {
        "trace": {"t1": store.get_existing("p2", {"trace": ["t1"]})["trace"]["t1"]},
        "observation": {},
        "score": {},
    }
    assert store.get_existing("p2", {"trace": ["t1"]})["trace"]["t1"].name == "two"
    store.refresh_trace_aggregates("p2", ["t1"])
    assert store.get_existing("p1", {"trace": ["t1"]})["trace"]["t1"].observation_count == 0
    assert store.delete_traces("p2", ["t1"]) == 1
    assert store.get_existing("p1", {"observation": ["o1"]})["observation"]["o1"]
    assert store.delete_traces("p2", ["t1"]) == 0
    store.update_trace_flags("p2", "t1", bookmarked=True)
    assert store.get_existing("p1", {"trace": ["t1"]})["trace"]["t1"].bookmarked is False


def test_every_public_method_takes_project_id_first(store):
    for name, fn in inspect.getmembers(type(store), inspect.isfunction):
        if name.startswith("_"):
            continue
        assert list(inspect.signature(fn).parameters)[:2] == ["self", "project_id"], name
