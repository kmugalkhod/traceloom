"""NFR-1 (lite): evaluation volume.

- rule matching: < 5 ms per trace with 50 enabled rules
- annotation claim: < 100 ms with a deep queue
- trace list filtered by a score: < 1 s with tens of thousands of scored traces
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from traceloom.annotation import claim
from traceloom.auth.permissions import Role
from traceloom.db.models import AnnotationItem, Score, Trace
from traceloom.evals.rules import RuleSnapshot, matches
from traceloom.ports import ScoreFilter, TraceQuery, TraceRow
from traceloom.prompts.service import Actor

pytestmark = pytest.mark.perf

RULES, TRACES_FOR_MATCHING = 50, 500
MATCH_MS, CLAIM_MS, LIST_S = 5.0, 100.0, 1.0
QUEUE_ITEMS, SCORED_TRACES = 20_000, 30_000
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def p95(samples: list[float]) -> float:
    return sorted(samples)[int(len(samples) * 0.95) - 1]


def test_rule_matching_is_under_5ms_per_trace_with_50_rules():
    rules = [
        RuleSnapshot(
            id=f"r{i}",
            project_id="p",
            target="trace",
            target_name=None,
            target_type=None,
            filter={
                "name": f"agent-{i % 7}",
                "environment": "production",
                "tags": [f"t{i % 5}", "x"],
                "metadata": {"tier": str(i % 3)},
            },
            sampling_pct=100.0,
            delay_seconds=0,
            template_version_id="v",
        )
        for i in range(RULES)
    ]
    traces = [
        TraceRow(
            f"t{i}",
            NOW,
            name=f"agent-{i % 7}",
            environment="production",
            tags=[f"t{i % 5}"],
            metadata={"tier": str(i % 3)},
        )
        for i in range(TRACES_FOR_MATCHING)
    ]
    per_trace, hits = [], 0
    for trace in traces:
        start = time.perf_counter()
        hits += sum(matches(rule, trace) for rule in rules)
        per_trace.append((time.perf_counter() - start) * 1000)
    assert hits > 0, "the rules never match: the benchmark would measure nothing"
    assert p95(per_trace) < MATCH_MS, f"p95 {p95(per_trace):.2f} ms"


def test_claim_is_under_100ms_on_a_deep_queue(client, db, world, container, make_queue):
    queue = make_queue(world.project)
    db.bulk_insert_mappings(
        AnnotationItem,
        [
            dict(
                id=f"i{n:06}",
                queue_id=queue.id,
                project_id=world.project.id,
                object_type="trace",
                object_id=f"trace-{n}",
                trace_id=f"trace-{n}",
                status="pending",
                created_at=NOW + timedelta(seconds=n),
            )
            for n in range(QUEUE_ITEMS)
        ],
    )
    db.commit()
    member = Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    samples = []
    for _ in range(40):
        start = time.perf_counter()
        item = claim.claim_next(db, world.project, member, queue.id, container.settings)
        samples.append((time.perf_counter() - start) * 1000)
        assert item is not None
        db.commit()
        claim.release(db, world.project, member, queue.id, item.id)
        db.commit()
    assert p95(samples) < CLAIM_MS, f"p95 {p95(samples):.1f} ms"


def test_score_filtered_trace_list_is_under_1s(client, db, world, container):
    project_id = world.project.id
    db.bulk_insert_mappings(
        Trace,
        [
            dict(
                project_id=project_id,
                id=f"t{n}",
                name=f"agent-{n % 7}",
                timestamp=NOW - timedelta(seconds=n),
                environment="production",
                tags=[],
                metadata_={},
                search_text="",
            )
            for n in range(SCORED_TRACES)
        ],
    )
    db.bulk_insert_mappings(
        Score,
        [
            dict(
                project_id=project_id,
                id=f"s{n}",
                trace_id=f"t{n}",
                name="relevance",
                value=(n % 100) / 100,
                data_type="NUMERIC",
                source="EVAL",
                timestamp=NOW - timedelta(seconds=n),
                environment="production",
            )
            for n in range(SCORED_TRACES)
        ],
    )
    db.commit()
    store = container.trace_store
    for query in (
        TraceQuery(score_filters=[ScoreFilter("relevance", "<", 0.5)], limit=50),
        TraceQuery(
            score_filters=[ScoreFilter("relevance", ">=", 0.2), ScoreFilter("relevance", "<", 0.4)],
            name="agent-3",
            limit=50,
        ),
        TraceQuery(score_filters=[ScoreFilter("relevance", "<", 0.5)], sort="score:relevance"),
    ):
        start = time.perf_counter()
        page = store.list_traces(project_id, query)
        elapsed = time.perf_counter() - start
        assert page.items, "the filter matched nothing: the benchmark would measure nothing"
        assert elapsed < LIST_S, f"{elapsed:.2f} s for {query.score_filters}"
