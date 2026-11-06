"""NFR-3 / AC-8: ingestion is durable when the queue consumer is down."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from traceloom.ingest.sweeper import Sweeper
from traceloom.main import create_app

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def batch_n(n: int) -> list[dict]:
    at = (T0 + timedelta(seconds=n)).isoformat()
    return [
        {
            "id": f"b{n}-t",
            "type": "trace-create",
            "timestamp": at,
            "body": {"id": f"t{n}", "name": f"trace {n}"},
        },
        {
            "id": f"b{n}-g",
            "type": "generation-create",
            "timestamp": at,
            "body": {
                "id": f"g{n}",
                "traceId": f"t{n}",
                "model": "gpt-4o",
                "usageDetails": {"input": 10, "output": 5},
            },
        },
        {
            "id": f"b{n}-s",
            "type": "score-create",
            "timestamp": at,
            "body": {"id": f"s{n}", "traceId": f"t{n}", "name": "q", "value": 1},
        },
    ]


@pytest.fixture
def headers(world, make_key):
    _, public, secret = make_key(world.project)
    return {"Authorization": "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()}


def counts(store, project_id):
    ids = {
        "trace": [f"t{n}" for n in range(3)],
        "observation": [f"g{n}" for n in range(3)],
        "score": [f"s{n}" for n in range(3)],
    }
    got = store.get_existing(project_id, ids)
    return {kind: len(rows) for kind, rows in got.items()}


def test_queue_stopped_then_replayed_exactly_once(client, container, settings, world, headers):
    container.queue.pause()
    for n in range(3):
        r = client.post("/api/public/ingestion", json={"batch": batch_n(n)}, headers=headers)
        assert r.status_code == 207 and len(r.json()["successes"]) == 3

    # Nothing processed yet, but everything is safely on disk.
    assert counts(container.trace_store, world.project.id) == {
        "trace": 0,
        "observation": 0,
        "score": 0,
    }
    assert len(list(container.blob_store.list("pending/"))) == 3
    assert container.queue.discard() == 3  # the process dies: queued messages are lost

    # Restart: a fresh app on the same database and blob folder replays what is pending.
    with TestClient(create_app(settings)) as restarted:
        restarted.app.state.container.queue.drain()
        again = restarted.app.state.container
        assert counts(again.trace_store, world.project.id) == {
            "trace": 3,
            "observation": 3,
            "score": 3,
        }
        assert list(again.blob_store.list("pending/")) == []
        assert len(list(again.blob_store.list("processed/"))) == 3

        # The sweeper finds nothing more, and a forced replay of a processed batch changes nothing.
        sweeper = Sweeper(again.blob_store, again.queue, 0, 1)
        assert sweeper.run_once(min_age=0) == 0
        key = next(k for k, _ in again.blob_store.list("processed/"))
        again.blob_store.move(key, key.replace("processed/", "pending/", 1))
        assert sweeper.run_once(min_age=0) == 1
        again.queue.drain()
        assert counts(again.trace_store, world.project.id) == {
            "trace": 3,
            "observation": 3,
            "score": 3,
        }
        trace = again.trace_store.get_existing(world.project.id, {"trace": ["t0", "t1", "t2"]})[
            "trace"
        ]
        assert all(t.observation_count == 1 and t.total_tokens == 15 for t in trace.values())


def test_sweeper_replays_a_batch_whose_enqueue_failed(
    client, container, world, headers, monkeypatch
):
    monkeypatch.setattr(
        container.queue, "enqueue", lambda m: (_ for _ in ()).throw(RuntimeError("down"))
    )
    r = client.post("/api/public/ingestion", json={"batch": batch_n(0)}, headers=headers)
    assert r.status_code == 207
    monkeypatch.undo()
    assert Sweeper(container.blob_store, container.queue, 0, 1).run_once(min_age=0) == 1
    container.queue.drain()
    assert counts(container.trace_store, world.project.id)["trace"] == 1


def test_sweeper_skips_young_batches_and_does_not_double_enqueue(client, container, headers):
    container.queue.pause()
    client.post("/api/public/ingestion", json={"batch": batch_n(0)}, headers=headers)
    container.queue.discard()
    sweeper = Sweeper(container.blob_store, container.queue, 3600, 1)
    assert sweeper.run_once() == 0  # younger than the configured age
    assert sweeper.run_once(min_age=0) == 1
    assert sweeper.run_once(min_age=0) == 0  # already enqueued by this sweeper
    assert container.queue.pending == 1
