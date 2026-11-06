"""ADR-0008 merge semantics, driven through the real event parser."""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime, timedelta

from traceloom.ingest.merge import EPOCH, Existing, merge_events, needed_ids
from traceloom.ingest.schemas import parse_event

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def ts(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def ev(type_: str, at: float, **body):
    return parse_event(
        {"id": f"e-{type_}-{at}-{len(body)}", "type": type_, "timestamp": ts(at), "body": body}
    )


def merge(*events, existing=None):
    return merge_events(list(events), existing or Existing())


def test_fill_older_event_only_fills_gaps():
    newer = ev("trace-create", 10, id="t1", name="new", user_id="u1")
    older = ev("trace-create", 1, id="t1", name="old", session_id="s1")
    row = merge(newer, older).traces["t1"]
    assert (row.name, row.user_id, row.session_id) == ("new", "u1", "s1")


def test_overwrite_newer_event_replaces_non_null_only():
    first = ev("trace-create", 1, id="t1", name="a", input={"q": 1})
    second = ev("trace-create", 5, id="t1", name="b")  # no input: must not erase it
    row = merge(first, second).traces["t1"]
    assert row.name == "b" and row.input == {"q": 1}
    assert row.event_ts == T0 + timedelta(seconds=5)


def test_input_dicts_are_replaced_not_merged_but_metadata_merges():
    a = ev("trace-create", 1, id="t1", input={"a": 1}, metadata={"x": 1, "y": 1})
    b = ev("trace-create", 2, id="t1", input={"b": 2}, metadata={"y": 2, "z": 3})
    row = merge(a, b).traces["t1"]
    assert row.input == {"b": 2}
    assert row.metadata == {"x": 1, "y": 2, "z": 3}
    older = ev("trace-create", 0, id="t1", metadata={"x": 9, "w": 4})
    assert merge(a, b, older).traces["t1"].metadata == {"x": 1, "y": 2, "z": 3, "w": 4}


def test_tags_union_regardless_of_order():
    a = ev("trace-create", 1, id="t1", tags=["a", "b"])
    b = ev("trace-create", 2, id="t1", tags=["b", "c"])
    assert sorted(merge(a, b).traces["t1"].tags) == ["a", "b", "c"]
    assert sorted(merge(b, a).traces["t1"].tags) == ["a", "b", "c"]


def test_idempotent_reapply_against_stored_rows():
    events = [
        ev("trace-create", 1, id="t1", name="n", tags=["a"], metadata={"k": 1}),
        ev(
            "generation-create",
            2,
            id="o1",
            trace_id="t1",
            name="g",
            start_time=ts(2),
            model="m",
            usage_details={"input": 3},
        ),
        ev("score-create", 3, id="s1", trace_id="t1", name="q", value=0.5),
    ]
    first = merge_events(events, Existing())
    stored = Existing(first.traces, first.observations, first.scores)
    second = merge_events(events, stored)
    assert second.traces == first.traces
    assert second.observations == first.observations
    assert second.scores == first.scores
    assert second.traces["t1"].tags == ["a"]


def test_update_before_create_has_both_sets_of_fields():
    update = ev("generation-update", 9, id="o1", trace_id="t1", output="answer", end_time=ts(9))
    create = ev(
        "generation-create",
        1,
        id="o1",
        trace_id="t1",
        name="gen",
        model="m",
        input="question",
        start_time=ts(1),
    )
    for order in ([update, create], [create, update]):
        obs = merge(*order).observations["o1"]
        assert (obs.output, obs.end_time) == ("answer", T0 + timedelta(seconds=9))
        assert (obs.name, obs.model, obs.input) == ("gen", "m", "question")
        assert obs.start_time == T0 + timedelta(seconds=1)
        assert obs.type == "generation"


def test_update_before_create_across_batches_via_stored_row():
    update = ev("span-update", 9, id="o1", trace_id="t1", output="out")
    stored = merge(update)
    create = ev("span-create", 1, id="o1", trace_id="t1", name="s", start_time=ts(1))
    obs = merge_events([create], Existing(stored.traces, stored.observations)).observations["o1"]
    assert (obs.output, obs.name, obs.start_time) == ("out", "s", T0 + timedelta(seconds=1))


def test_any_event_order_gives_the_same_rows():
    events = [
        ev("span-create", 1, id="o1", trace_id="t1", name="root", start_time=ts(1), input="i"),
        ev("span-update", 4, id="o1", output="o", end_time=ts(4), level="WARNING"),
        ev("generation-create", 2, id="o2", trace_id="t1", parent_observation_id="o1", model="m"),
        ev("trace-create", 0, id="t1", name="trace", tags=["x"]),
        ev("score-create", 5, id="s1", trace_id="t1", name="q", value=True),
    ]
    baseline = merge_events(events, Existing())
    rng = random.Random(7)
    for _ in range(25):
        shuffled = events[:]
        rng.shuffle(shuffled)
        got = merge_events(shuffled, Existing())
        assert got.traces == baseline.traces
        assert got.observations == baseline.observations
        assert got.scores == baseline.scores


def test_provided_usage_and_cost_replace_derived_values():
    create = ev("generation-create", 1, id="o1", trace_id="t1", model="m", start_time=ts(1))
    first = merge(create)
    obs = first.observations["o1"]
    obs.usage_details, obs.usage_source = {"input": 1}, "tokenizer"
    obs.cost_details, obs.cost_source = {"total": 1.0}, "calculated"
    update = ev(
        "generation-update",
        2,
        id="o1",
        usage_details={"input": 10, "output": 5},
        cost_details={"total": 0.5},
    )
    got = merge_events([update], Existing(first.traces, first.observations)).observations["o1"]
    assert got.usage_details == {"input": 10, "output": 5} and got.usage_source == "provided"
    assert got.cost_details == {"total": 0.5} and got.cost_source == "provided"


def test_orphan_observation_gets_stub_trace():
    obs = ev(
        "span-create",
        3,
        id="o1",
        trace_id="missing",
        name="child",
        parent_observation_id="p0",
        start_time=ts(3),
    )
    result = merge(obs)
    stub = result.traces["missing"]
    assert stub.event_ts == EPOCH and stub.timestamp == T0 + timedelta(seconds=3)
    assert stub.name is None  # a non-root span does not name the trace
    # A real trace-create (even an old one) then overwrites the stub.
    real = ev("trace-create", 1, id="missing", name="real")
    merged = merge_events([real], Existing(result.traces, result.observations))
    assert merged.traces["missing"].name == "real"


def test_root_span_without_trace_fields_creates_trace_from_span():
    root = ev(
        "span-create", 2, id="root1", name="agent run", input="hi", output="bye", start_time=ts(2)
    )
    result = merge(root)
    assert result.observations["root1"].trace_id == "root1"
    trace = result.traces["root1"]
    assert (trace.name, trace.input, trace.output) == ("agent run", "hi", "bye")
    assert trace.timestamp == T0 + timedelta(seconds=2)


def test_explicit_trace_fields_are_not_overwritten_by_root_span():
    trace = ev("trace-create", 1, id="t1", name="explicit", input="x")
    root = ev("span-create", 2, id="r", trace_id="t1", name="span-name", input="y", output="out")
    row = merge(trace, root).traces["t1"]
    assert (row.name, row.input, row.output) == ("explicit", "x", "out")


def test_bare_observation_id_is_provisional_until_explicit_trace_id():
    update = ev("span-update", 9, id="o1", output="o")
    create = ev("span-create", 1, id="o1", trace_id="t1")
    assert merge(update).observations["o1"].trace_id == "o1"
    assert merge(update, create).observations["o1"].trace_id == "t1"


def test_scores_map_values_and_types():
    rows = merge(
        ev("score-create", 1, id="a", trace_id="t", name="n", value=0.25),
        ev("score-create", 1, id="b", trace_id="t", name="n", value="good"),
        ev("score-create", 1, id="c", trace_id="t", name="n", value=True),
    ).scores
    assert (rows["a"].value, rows["a"].data_type) == (0.25, "NUMERIC")
    assert (rows["b"].string_value, rows["b"].data_type) == ("good", "CATEGORICAL")
    assert (rows["c"].value, rows["c"].data_type) == (1.0, "BOOLEAN")


def test_needed_ids_includes_trace_of_observations():
    events = [ev("span-create", 1, id="o1", trace_id="t1"), ev("trace-create", 1, id="t2")]
    assert needed_ids(events) == {"trace": ["t1", "t2"], "observation": ["o1"]}


def test_inputs_are_not_mutated():
    first = merge(ev("trace-create", 1, id="t1", name="a", tags=["x"]))
    stored = Existing(first.traces)
    before = repr(first.traces["t1"])
    merge_events([ev("trace-create", 5, id="t1", name="b", tags=["y"])], stored)
    assert repr(first.traces["t1"]) == before
    assert list(itertools.chain(first.traces)) == ["t1"]
