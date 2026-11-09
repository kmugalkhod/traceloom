"""Score filters, score sorts, aggregates and analytics reads: lite always, ClickHouse under
TRACELOOM_IT=1."""

from __future__ import annotations

import pytest

from tests.contract.stores import minutes
from traceloom.ports import (
    ScoreFilter,
    ScoreRange,
    ScoreSelector,
    SessionQuery,
    TraceQuery,
)

P = "proj-scores"


def sc(id, name, value=None, string=None, data_type="NUMERIC", t=1, **extra):
    return {
        "id": id,
        "name": name,
        "value": value,
        "string_value": string,
        "data_type": data_type,
        "timestamp": minutes(t),
        **extra,
    }


@pytest.fixture(autouse=True)
def rows(seed):
    seed(P, "a", timestamp=minutes(1), session_id="s1", scores=[
        sc("a1", "quality", 0.9), sc("a2", "quality", 0.7, t=2),
        sc("a3", "ok", 1.0, data_type="BOOLEAN"),
        sc("a4", "tone", string="warm", data_type="CATEGORICAL"),
    ])  # fmt: skip
    seed(P, "b", timestamp=minutes(2), session_id="s1", scores=[
        sc("b1", "quality", 0.3), sc("b2", "ok", 0.0, data_type="BOOLEAN"),
        sc("b3", "tone", string="cold", data_type="CATEGORICAL"),
    ])  # fmt: skip
    seed(P, "c", timestamp=minutes(3), session_id="s2", scores=[sc("c1", "quality", 0.5)])
    seed(P, "d", timestamp=minutes(4), session_id="s3")  # no scores
    seed(
        P,
        "e",
        timestamp=minutes(5),
        environment="traceloom-eval",
        scores=[sc("e1", "quality", 1.0)],
    )
    seed(P, "x", timestamp=minutes(6), environment="traceloom-experiment")


def ids(store, **kw):
    kw.setdefault("limit", 100)
    return [t.id for t in store.list_traces(P, TraceQuery(**kw)).items]


def f(name, op, value=None):
    return ScoreFilter(name, op, value)


@pytest.mark.parametrize(
    ("flt", "expected"),
    [
        (f("quality", ">", 0.6), {"a", "e"}),
        (f("quality", ">=", 0.7), {"a", "e"}),
        (f("quality", "<", 0.5), {"b"}),
        (f("quality", "<=", 0.5), {"b", "c"}),
        (f("quality", "=", 0.5), {"c"}),
        (f("quality", "!=", 0.5), {"a", "b", "e"}),
        (f("quality", "in", [0.3, 0.5]), {"b", "c"}),
        (f("ok", "=", True), {"a"}),
        (f("ok", "=", False), {"b"}),
        (f("tone", "in", ["warm", "hot"]), {"a"}),
        (f("tone", "=", "cold"), {"b"}),
        (f("tone", "!=", "cold"), {"a"}),
        (f("quality", "exists"), {"a", "b", "c", "e"}),
        (f("tone", "missing"), {"c", "d", "e", "x"}),
    ],
)
def test_filter_ops(read_store, flt, expected):
    got = set(ids(read_store, score_filters=[flt], exclude_environments=()))
    assert got == expected


def test_filters_combine_with_and(read_store):
    got = ids(read_store, score_filters=[f("quality", ">", 0.2), f("ok", "=", True)])
    assert got == ["a"]


def test_invalid_filters_are_rejected():
    for bad in (("q", "~", 1), ("q", "in", []), ("q", ">", "x"), ("q", "=", None), ("", "=", 1)):
        with pytest.raises(ValueError):
            ScoreFilter(*bad)


def test_score_sort_puts_unscored_last_both_directions(read_store):
    desc = ids(
        read_store,
        sort="score:quality",
        descending=True,
        exclude_environments=("traceloom-eval", "traceloom-experiment"),
    )
    assert desc == ["a", "c", "b", "d"]  # a averages 0.8, c 0.5, b 0.3; d has none
    asc = ids(
        read_store,
        sort="score:quality",
        descending=False,
        exclude_environments=("traceloom-eval", "traceloom-experiment"),
    )
    assert asc == ["b", "c", "a", "d"]


def test_score_sort_pages_with_a_cursor(read_store):
    seen, cursor = [], None
    while True:
        page = read_store.list_traces(
            P,
            TraceQuery(
                sort="score:quality",
                limit=2,
                cursor=cursor,
                exclude_environments=("traceloom-eval", "traceloom-experiment"),
            ),
        )
        seen += [t.id for t in page.items]
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == ["a", "c", "b", "d"]


def test_eval_and_experiment_environments_hidden_unless_selected(read_store):
    hidden = ("traceloom-eval", "traceloom-experiment")
    assert set(ids(read_store, exclude_environments=hidden)) == {"a", "b", "c", "d"}
    assert set(ids(read_store)) == {"a", "b", "c", "d", "e", "x"}
    assert ids(read_store, environment="traceloom-eval", exclude_environments=hidden) == ["e"]
    stats = read_store.trace_stats(P, TraceQuery(exclude_environments=hidden))
    assert stats.count == 4
    sessions = read_store.list_sessions(P, SessionQuery(exclude_environments=hidden)).items
    assert {s.session_id for s in sessions} == {"s1", "s2", "s3"}


def test_session_filter_matches_sessions_with_a_matching_trace(read_store):
    page = read_store.list_sessions(P, SessionQuery(score_filters=[f("ok", "=", False)]))
    assert [s.session_id for s in page.items] == ["s1"]
    assert page.items[0].trace_count == 2  # the session keeps all its traces


def test_score_aggregates_are_per_object_averages(read_store):
    got = read_store.score_aggregates(P, "trace", ["a", "b", "d"], ["quality", "ok", "tone"])
    assert got["a"]["quality"].average == pytest.approx(0.8) and got["a"]["quality"].count == 2
    assert got["a"]["ok"].average == 1.0
    assert got["a"]["tone"].average is None and got["a"]["tone"].latest_string == "warm"
    assert got["b"]["quality"].average == pytest.approx(0.3)
    assert "d" not in got
    only = read_store.score_aggregates(P, "trace", ["a"], ["ok"])
    assert set(only["a"]) == {"ok"}
    assert read_store.score_aggregates("other-project", "trace", ["a"]) == {}
    with pytest.raises(ValueError):
        read_store.score_aggregates(P, "bogus", ["a"])


def test_distribution_and_trend(read_store):
    sel = ScoreSelector("quality")
    points = read_store.score_distribution(P, sel, ScoreRange())
    assert sorted(p.value for p in points) == [0.3, 0.5, 0.7, 0.9, 1.0]
    assert [p.timestamp for p in points] == sorted((p.timestamp for p in points), reverse=True)
    window = ScoreRange(from_time=minutes(2), to_time=minutes(3))
    assert [p.value for p in read_store.score_trend(P, sel, window)] == [0.7]  # only a2 is at t=2
    trend = read_store.score_trend(P, sel, ScoreRange())
    assert [p.timestamp for p in trend] == sorted(p.timestamp for p in trend)  # oldest first
    assert read_store.score_distribution(P, ScoreSelector("quality", "EVAL"), ScoreRange()) == []
    assert len(read_store.score_distribution(P, sel, ScoreRange(), limit=2)) == 2
    assert [
        p.string_value
        for p in read_store.score_distribution(P, ScoreSelector("tone"), ScoreRange())
    ] != []


def test_score_counts_per_config(read_store, seed):
    seed(
        P,
        "cfg",
        scores=[sc("k1", "quality", 1.0, config_id="c1"), sc("k2", "ok", 1.0, config_id="c1")],
    )
    seed(P, "cfg2", scores=[sc("k3", "ok", 0.0, config_id="c2")])
    assert read_store.score_counts(P, ["c1", "c2", "c3"]) == {"c1": 2, "c2": 1}
    assert read_store.score_counts(P, []) == {}
    assert read_store.score_counts("other-project", ["c1"]) == {}


def test_pairs_match_on_the_trace(read_store):
    pairs = read_store.score_pairs(P, ScoreSelector("quality"), ScoreSelector("ok"), ScoreRange())
    by_trace = {}
    for p in pairs:
        by_trace.setdefault(p.trace_id, []).append((p.a_value, p.b_value))
    assert sorted(by_trace["a"]) == [(0.7, 1.0), (0.9, 1.0)]
    assert by_trace["b"] == [(0.3, 0.0)]
    assert set(by_trace) == {"a", "b"}  # c has no "ok" score
    cats = read_store.score_pairs(P, ScoreSelector("ok"), ScoreSelector("tone"), ScoreRange())
    assert {(p.trace_id, p.b_string) for p in cats} == {("a", "warm"), ("b", "cold")}


def test_clickhouse_sql_for_filters_and_sort_is_project_scoped():
    """Offline check of the ClickHouse builders (the parity run needs TRACELOOM_IT=1)."""
    from traceloom.adapters.full import clickhouse_trace_store as ch

    query = TraceQuery(
        score_filters=[f("quality", ">", 0.5), f("tone", "missing")],
        sort="score:quality",
        exclude_environments=("traceloom-eval",),
    )
    sql, params = ch.build_list_traces(P, query, "db.traces")
    assert sql.count("FROM db.scores FINAL WHERE project_id = {project_id:String}") == 3
    assert "id IN (SELECT trace_id" in sql and "id NOT IN (SELECT trace_id" in sql
    assert "LEFT JOIN" in sql and "join_use_nulls = 1" in sql
    assert params["score_sort_name"] == "quality" and params["exclude_envs"] == ["traceloom-eval"]
    plain, _ = ch.build_list_traces(P, TraceQuery(), "db.traces")
    assert "scores" not in plain and "NOT IN" not in plain
    sessions, _ = ch.build_list_sessions(P, SessionQuery(score_filters=[f("ok", "=", True)]))
    assert "session_id IN (SELECT session_id FROM traces FINAL" in sessions
