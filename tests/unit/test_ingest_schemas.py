from datetime import UTC, datetime

import pytest

from traceloom.ingest.schemas import OBSERVATION_TYPES, EventError, parse_event


def event(type_, body=None, **extra):
    return {
        "id": "e1",
        "type": type_,
        "timestamp": "2026-11-06T12:00:00Z",
        "body": body or {},
        **extra,
    }


@pytest.mark.parametrize(
    ("type_", "kind", "op", "default"),
    [
        ("trace-create", "trace", "create", None),
        ("span-create", "observation", "create", "span"),
        ("span-update", "observation", "update", "span"),
        ("generation-create", "observation", "create", "generation"),
        ("generation-update", "observation", "update", "generation"),
        ("event-create", "observation", "create", "event"),
        ("observation-create", "observation", "create", None),
        ("observation-update", "observation", "update", None),
    ],
)
def test_event_types(type_, kind, op, default):
    parsed = parse_event(event(type_))
    assert (parsed.kind, parsed.op, parsed.default_type) == (kind, op, default)
    assert parsed.entity_id == "e1"  # falls back to the event id


def test_score_event():
    parsed = parse_event(event("score-create", {"traceId": "t1", "name": "q", "value": 0.5}))
    assert parsed.kind == "score" and parsed.body.trace_id == "t1"


@pytest.mark.parametrize("obs_type", OBSERVATION_TYPES)
def test_observation_create_accepts_every_type(obs_type):
    assert parse_event(event("observation-create", {"type": obs_type})).body.type == obs_type


def test_camel_and_snake_case_both_accepted():
    a = parse_event(event("generation-create", {"traceId": "t", "usageDetails": {"input": 1}}))
    b = parse_event(event("generation-create", {"trace_id": "t", "usage_details": {"input": 1}}))
    assert a.body == b.body


def test_timestamps_become_aware_utc():
    parsed = parse_event(event("trace-create", {"timestamp": "2026-11-06T12:00:00"}))
    assert parsed.body.timestamp == datetime(2026, 11, 6, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "string",
        {},
        event("nope"),
        {**event("trace-create"), "id": ""},
        {**event("trace-create"), "timestamp": "yesterday"},
        event("trace-create", {"tags": "not-a-list"}),
        event("span-create", {"level": "LOUD"}),
        event("observation-create", {"type": "wizard"}),
        event("generation-create", {"usage_details": {"input": -1}}),
        event("generation-create", {"usage_details": {"input": "many"}}),
        event("score-create", {"name": "q", "value": 1}),  # no trace/session
        event("score-create", {"trace_id": "t", "value": 1}),  # no name
    ],
)
def test_invalid_events_raise(bad):
    with pytest.raises(EventError):
        parse_event(bad)


def test_unknown_body_fields_are_ignored():
    assert parse_event(event("trace-create", {"name": "n", "futureField": 1})).body.name == "n"
