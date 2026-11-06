"""Read-side contract for TraceStore: list filters, search, sort, cursor paging and detail."""

from __future__ import annotations

import time

import pytest

from tests.contract.stores import T0, minutes
from traceloom.ports import InvalidCursor, TraceQuery

P1, P2 = "proj1", "proj2"


def ids(page) -> list[str]:
    return [t.id for t in page.items]


def test_search_is_case_insensitive_over_name_id_input_output(read_store, seed):
    seed(P1, "t-alpha", name="Checkout", input={"q": "Refund POLICY"}, output="done")
    seed(P1, "t-beta", name="other", output="the Banana split")
    seed(P1, "t-gamma", name="other")
    q = lambda text: ids(read_store.list_traces(P1, TraceQuery(q=text)))  # noqa: E731
    assert q("banana") == ["t-beta"]
    assert q("refund policy") == ["t-alpha"]
    assert q("CHECKOUT") == ["t-alpha"]
    assert q("gamma") == ["t-gamma"]


def test_search_treats_wildcards_literally(read_store, seed):
    seed(P1, "a", output="100% sure")
    seed(P1, "b", output="plain")
    assert ids(read_store.list_traces(P1, TraceQuery(q="%"))) == ["a"]
    assert ids(read_store.list_traces(P1, TraceQuery(q="_"))) == []


def test_exact_filters(read_store, seed):
    seed(P1, "a", user_id="u1", session_id="s1", environment="prod", release="r1", version="v1")
    seed(P1, "b", user_id="u2", session_id="s2", environment="dev", release="r2", version="v2")
    for field, value in [
        ("name", "a"),
        ("user_id", "u1"),
        ("session_id", "s1"),
        ("environment", "prod"),
        ("release", "r1"),
        ("version", "v1"),
    ]:
        assert ids(read_store.list_traces(P1, TraceQuery(**{field: value}))) == ["a"], field


def test_tags_match_any_of_and_whole_tag_only(read_store, seed):
    seed(P1, "a", tags=["billing", "eu"])
    seed(P1, "b", tags=["support"])
    seed(P1, "c", tags=["billing-extra"])
    page = read_store.list_traces(P1, TraceQuery(tags=["billing", "support"]))
    assert sorted(ids(page)) == ["a", "b"]
    assert ids(read_store.list_traces(P1, TraceQuery(tags=["bill"]))) == []


def test_level_and_has_error(read_store, seed):
    for tid, level in [("d", "DEBUG"), ("n", "DEFAULT"), ("w", "WARNING"), ("e", "ERROR")]:
        seed(P1, tid, max_level=level)
    assert sorted(ids(read_store.list_traces(P1, TraceQuery(min_level="WARNING")))) == ["e", "w"]
    assert sorted(ids(read_store.list_traces(P1, TraceQuery(has_error=True)))) == ["e"]
    assert sorted(ids(read_store.list_traces(P1, TraceQuery(has_error=False)))) == ["d", "n", "w"]
    with pytest.raises(ValueError):
        read_store.list_traces(P1, TraceQuery(min_level="LOUD"))


def test_metadata_ranges_bookmark_and_dates(read_store, seed):
    seed(P1, "a", metadata={"region": "eu"}, latency_ms=100, total_cost=0.5, timestamp=minutes(10))
    seed(P1, "b", metadata={"region": "us"}, latency_ms=900, total_cost=2.0, timestamp=minutes(20))
    seed(P1, "c", metadata={"region": "eu"}, latency_ms=300, total_cost=1.0, bookmarked=True,
         timestamp=minutes(30))  # fmt: skip
    q = lambda **kw: sorted(ids(read_store.list_traces(P1, TraceQuery(**kw))))  # noqa: E731
    assert q(metadata={"region": "eu"}) == ["a", "c"]
    assert q(metadata={"region": "eu"}, from_time=minutes(15), to_time=minutes(40)) == ["c"]
    assert q(latency_min_ms=200, latency_max_ms=500) == ["c"]
    assert q(cost_min=1.0) == ["b", "c"]
    assert q(cost_max=0.5) == ["a"]
    assert q(bookmarked=True) == ["c"]
    assert q(bookmarked=False) == ["a", "b"]
    assert q(metadata={"region": "eu", "missing": "x"}) == []


@pytest.mark.parametrize("sort", ["timestamp", "latency", "cost", "tokens"])
@pytest.mark.parametrize("descending", [True, False])
def test_cursor_pagination_has_no_duplicates_or_gaps(read_store, seed, sort, descending):
    # Many ties (values repeat) to exercise the id tie-breaker; some NULL latencies.
    for i in range(23):
        seed(
            P1,
            f"t{i:02d}",
            timestamp=minutes(i % 5),
            latency_ms=None if i % 7 == 0 else float(i % 4),
            total_cost=float(i % 3),
            total_tokens=i % 6,
        )
    full = read_store.list_traces(P1, TraceQuery(sort=sort, descending=descending, limit=200))
    assert len(full.items) == 23 and full.next_cursor is None
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = read_store.list_traces(
            P1, TraceQuery(sort=sort, descending=descending, limit=5, cursor=cursor)
        )
        seen += ids(page)
        pages += 1
        cursor = page.next_cursor
        if cursor is None:
            break
    assert pages == 5
    assert seen == ids(full)
    assert len(set(seen)) == 23


def test_default_page_size_is_50_and_newest_first(read_store, seed):
    for i in range(55):
        seed(P1, f"t{i:02d}", timestamp=minutes(i))
    page = read_store.list_traces(P1, TraceQuery())
    assert len(page.items) == 50 and page.next_cursor
    assert page.items[0].id == "t54"
    rest = read_store.list_traces(P1, TraceQuery(cursor=page.next_cursor))
    assert len(rest.items) == 5 and rest.next_cursor is None


def test_bad_cursors_are_rejected(read_store, seed):
    seed(P1, "a")
    seed(P1, "b", timestamp=minutes(1))
    with pytest.raises(InvalidCursor):
        read_store.list_traces(P1, TraceQuery(cursor="garbage"))
    page = read_store.list_traces(P1, TraceQuery(limit=1))
    with pytest.raises(InvalidCursor):  # a time cursor cannot drive a cost sort
        read_store.list_traces(P1, TraceQuery(sort="cost", cursor=page.next_cursor))
    with pytest.raises(ValueError):
        read_store.list_traces(P1, TraceQuery(sort="nonsense"))


def test_list_is_project_scoped(read_store, seed):
    seed(P1, "mine", output="needle")
    seed(P2, "theirs", output="needle")
    page = read_store.list_traces(P1, TraceQuery(q="needle"))
    assert ids(page) == ["mine"]
    assert ids(read_store.list_traces("nobody", TraceQuery())) == []


def test_list_attaches_scores(read_store, seed):
    seed(P1, "a", scores=[{"id": "s1", "name": "quality", "timestamp": T0, "value": 0.9}])
    seed(P1, "b")
    by_id = {t.id: t for t in read_store.list_traces(P1, TraceQuery()).items}
    assert [s.name for s in by_id["a"].scores] == ["quality"]
    assert by_id["b"].scores == []


def test_get_trace_returns_tree_scores_and_rollups(read_store, seed):
    seed(
        P1,
        "t1",
        input={"messages": [{"role": "user", "content": "hi"}]},
        output="bye",
        tags=["x"],
        metadata={"k": 1},
        latency_ms=1500.0,
        total_tokens=42,
        total_cost=0.25,
        max_level="ERROR",
        observation_count=3,
        observations=[
            {
                "id": "o2",
                "parent_observation_id": "o1",
                "type": "generation",
                "model": "m",
                "start_time": minutes(1),
                "usage_details": {"input": 3},
                "cost_details": {"input": 0.1},
                "input": {"a": 1},
            },
            {"id": "o1", "start_time": T0, "name": "root"},
            {"id": "o3", "parent_observation_id": "o1", "start_time": minutes(1), "level": "ERROR"},
        ],  # fmt: skip
        scores=[
            {"id": "s2", "name": "b", "timestamp": minutes(2), "value": 1.0},
            {
                "id": "s1",
                "name": "a",
                "timestamp": T0,
                "string_value": "ok",
                "data_type": "CATEGORICAL",
                "observation_id": "o2",
            },
        ],  # fmt: skip
    )
    detail = read_store.get_trace(P1, "t1")
    assert detail is not None
    assert [o.id for o in detail.observations] == ["o1", "o2", "o3"]
    gen = detail.observations[1]
    assert (gen.parent_observation_id, gen.model, gen.usage_details) == ("o1", "m", {"input": 3})
    assert gen.input == {"a": 1}
    assert [s.id for s in detail.scores] == ["s1", "s2"]
    t = detail.trace
    assert t.input == {"messages": [{"role": "user", "content": "hi"}]}
    assert t.output == "bye"
    assert (t.latency_ms, t.total_tokens, t.total_cost, t.max_level, t.observation_count) == (
        1500.0, 42, 0.25, "ERROR", 3
    )  # fmt: skip
    assert t.metadata == {"k": 1} and t.tags == ["x"]


def test_get_trace_never_crosses_projects(read_store, seed):
    seed(
        P1,
        "shared-id",
        observations=[{"id": "o", "start_time": T0}],
        scores=[{"id": "s", "name": "n", "timestamp": T0, "value": 1.0}],
    )
    seed(P2, "shared-id", observations=[{"id": "o", "start_time": T0}])
    assert read_store.get_trace("other", "shared-id") is None
    mine = read_store.get_trace(P1, "shared-id")
    theirs = read_store.get_trace(P2, "shared-id")
    assert len(mine.scores) == 1 and theirs.scores == []
    assert read_store.get_trace(P1, "missing") is None


def test_first_page_is_fast_on_10k_traces(lite_session_factory):
    from tests.contract.stores import LiteReadStore
    from traceloom.db.models import Trace

    store = LiteReadStore(lite_session_factory)
    rows = [
        {
            "project_id": P1,
            "id": f"t{i:05d}",
            "name": f"trace {i}",
            "timestamp": minutes(i),
            "input": "null",
            "output": '"some output text"',
            "tags": [],
            "metadata": {},
            "search_text": f"trace {i} some output text " * 20,
            "latency_ms": float(i % 977),
            "total_cost": (i % 13) / 10,
            "total_tokens": i,
        }
        for i in range(10_000)
    ]
    with lite_session_factory() as session:
        session.execute(Trace.__table__.insert(), rows)
        session.commit()
    for query in (
        TraceQuery(),
        TraceQuery(sort="latency"),
        TraceQuery(q="output text 9999"),
        TraceQuery(environment="default", tags=["x"], metadata={"a": "b"}),
    ):
        start = time.perf_counter()
        store.list_traces(P1, query)
        assert time.perf_counter() - start < 1.0, query
