"""prompt_metrics and list_prompt_generations: lite always, ClickHouse under TRACELOOM_IT=1."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.contract.stores import minutes
from traceloom.ports import PromptMetricsQuery

P = "proj-prompts"


def gen(oid, minute, version=2, name="support", **fields):
    start = minutes(minute)
    fields.setdefault("type", "generation")
    fields.setdefault("end_time", start + timedelta(milliseconds=500))
    return {
        "id": oid,
        "start_time": start,
        "prompt_name": name,
        "prompt_version": version,
        "usage_details": {"total": 100},
        "cost_details": {"total": 0.25},
        "model": "gpt-4o",
        **fields,
    }


def seed_set(seed):
    seed(
        P,
        "t1",
        timestamp=minutes(1),
        observations=[
            gen("g1", 1, end_time=minutes(1) + timedelta(milliseconds=100)),
            gen("g2", 2, end_time=minutes(2) + timedelta(milliseconds=300), environment="staging"),
            gen("g3", 3, usage_details={"input": 30, "output": 20}, cost_details={}),
            gen("g4", 4, end_time=None),  # still running: no latency
            gen("other", 6, name="billing"),
            gen("span", 7, type="span"),
            {"id": "plain", "start_time": minutes(8), "type": "generation"},
        ],
        scores=[
            {"id": "s1", "name": "quality", "value": 1.0, "timestamp": minutes(1)},
            {"id": "s2", "name": "quality", "value": 0.0, "timestamp": minutes(2)},
            {"id": "s3", "name": "helpful", "value": 0.5, "timestamp": minutes(2)},
            {
                "id": "s4",
                "name": "tone",
                "value": None,
                "string_value": "polite",
                "data_type": "CATEGORICAL",
                "timestamp": minutes(2),
            },
        ],
    )
    seed(
        P,
        "t2",
        timestamp=minutes(10),
        observations=[gen("g5", 10, version=1), gen("v1", 11, version=1)],
        scores=[{"id": "s5", "name": "quality", "value": 0.5, "timestamp": minutes(10)}],
    )
    seed(
        "other",
        "t9",
        timestamp=minutes(1),
        observations=[gen("leak", 1, usage_details={"total": 9999}, cost_details={"total": 99.0})],
        scores=[{"id": "s9", "name": "quality", "value": 9.0, "timestamp": minutes(1)}],
    )


def test_metrics_count_only_matching_name_and_version(read_store, seed):
    seed_set(seed)
    m = read_store.prompt_metrics(P, "support", 2)
    assert m.count == 4  # g1..g4; not v1, billing, the span or the unlinked generation
    assert m.total_tokens == 100 * 3 + 50  # g3 falls back to input + output
    assert m.total_cost == pytest.approx(0.75)  # g3 has no cost keys
    assert m.median_latency_ms == pytest.approx(300.0)  # 100, 300, 500; g4 has no end time
    assert read_store.prompt_metrics(P, "support", 1).count == 2
    assert read_store.prompt_metrics(P, "support", None).count == 6
    assert read_store.prompt_metrics(P, "support", 99).count == 0


def test_scores_average_per_numeric_name(read_store, seed):
    seed_set(seed)
    m = read_store.prompt_metrics(P, "support", 2)
    assert m.scores == {"helpful": pytest.approx(0.5), "quality": pytest.approx(0.5)}
    assert read_store.prompt_metrics(P, "support", 1).scores == {"quality": pytest.approx(0.5)}


def test_empty_metrics(read_store, seed):
    seed_set(seed)
    m = read_store.prompt_metrics(P, "nothing", 1)
    assert (m.count, m.total_tokens, m.total_cost) == (0, 0, 0.0)
    assert m.median_latency_ms is None and m.scores == {}


def test_queries_are_project_scoped(read_store, seed):
    seed_set(seed)
    m = read_store.prompt_metrics("other", "support", 2)
    assert (m.count, m.total_tokens) == (1, 9999)
    assert m.scores == {"quality": pytest.approx(9.0)}
    assert [
        g.observation_id for g in read_store.list_prompt_generations("other", "support", 2)
    ] == ["leak"]
    assert read_store.prompt_metrics("nobody", "support", 2).count == 0


def test_date_range_and_environment_filters(read_store, seed):
    seed_set(seed)
    window = PromptMetricsQuery(from_time=minutes(2), to_time=minutes(3))
    assert read_store.prompt_metrics(P, "support", 2, window).count == 2
    staging = PromptMetricsQuery(environment="staging")
    m = read_store.prompt_metrics(P, "support", 2, staging)
    assert m.count == 1 and m.median_latency_ms == pytest.approx(300.0)
    ids = [
        g.observation_id for g in read_store.list_prompt_generations(P, "support", 2, 50, staging)
    ]
    assert ids == ["g2"]


def test_generations_are_newest_first_and_limited(read_store, seed):
    seed_set(seed)
    rows = read_store.list_prompt_generations(P, "support", 2)
    assert [g.observation_id for g in rows] == ["g4", "g3", "g2", "g1"]
    first = rows[-1]
    assert (first.trace_id, first.version, first.model) == ("t1", 2, "gpt-4o")
    assert first.latency_ms == pytest.approx(100.0)
    assert (first.tokens, first.cost) == (100, pytest.approx(0.25))
    assert rows[0].latency_ms is None and rows[1].tokens == 50
    assert [g.observation_id for g in read_store.list_prompt_generations(P, "support", 2, 2)] == [
        "g4",
        "g3",
    ]
