"""QA adversarial checks for score filters, sorts and analytics (T-7); lite store."""

from __future__ import annotations

import pytest

from tests.contract.stores import minutes
from traceloom.ports import ScoreFilter, ScoreRange, ScoreSelector, TraceQuery

P = "qa-scores-a"
OTHER = "qa-scores-b"
NASTY = "q'; DROP TABLE scores; --"


def sc(id, name, value=None, string=None, data_type="NUMERIC", t=1):
    return {
        "id": id,
        "name": name,
        "value": value,
        "string_value": string,
        "data_type": data_type,
        "timestamp": minutes(t),
    }


@pytest.fixture(autouse=True)
def rows(seed):
    seed(P, "a", timestamp=minutes(1), scores=[sc("a1", NASTY, 0.9), sc("a2", "q", 0.2)])
    seed(P, "b", timestamp=minutes(2), scores=[sc("b1", NASTY, 0.1)])
    seed(OTHER, "z", timestamp=minutes(3), scores=[sc("z1", NASTY, 1.0), sc("z2", "q", 1.0)])


def ids(store, project=P, **kw):
    kw.setdefault("limit", 100)
    return [t.id for t in store.list_traces(project, TraceQuery(**kw)).items]


def test_score_names_with_quotes_are_data_not_sql(read_store):
    assert ids(read_store, score_filters=[ScoreFilter(NASTY, ">", 0.5)]) == ["a"]
    assert ids(read_store, sort="score:" + NASTY, descending=True) == ["a", "b"]
    assert read_store.list_traces(P, TraceQuery(limit=10)).items  # table still there


def test_filters_and_sorts_never_cross_projects(read_store):
    assert ids(read_store, score_filters=[ScoreFilter("q", ">=", 0.0)]) == ["a"]
    assert ids(read_store, project=OTHER, score_filters=[ScoreFilter("q", ">=", 0.0)]) == ["z"]
    got = read_store.score_aggregates(P, "trace", ["a", "z"], ["q"])
    assert set(got) == {"a"}
    sel = ScoreSelector("q")
    assert [p.value for p in read_store.score_distribution(P, sel, ScoreRange())] == [0.2]


def test_filter_on_unknown_score_matches_nothing_and_missing_matches_all(read_store):
    assert ids(read_store, score_filters=[ScoreFilter("nope", "exists")]) == []
    assert sorted(ids(read_store, score_filters=[ScoreFilter("nope", "missing")])) == ["a", "b"]
    assert ids(read_store, score_filters=[ScoreFilter("nope", "!=", 1)]) == []
