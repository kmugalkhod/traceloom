"""Queue messages dispatch by `type`; a message without one is an ingest batch."""

from __future__ import annotations

import pytest

from traceloom.adapters.lite.inproc_queue import InProcQueue
from traceloom.ingest.sweeper import Sweeper
from traceloom.queue_registry import (
    INGEST,
    WEBHOOK_DELIVERY,
    HandlerRegistry,
    UnknownMessageType,
)


def test_registry_defaults_to_ingest():
    seen = []
    registry = HandlerRegistry()
    registry.register(INGEST, lambda m: seen.append(("ingest", dict(m))))
    registry.dispatch({"blob_key": "a"})
    registry.dispatch({"type": None, "blob_key": "b"})
    assert [k for k, _ in seen] == ["ingest", "ingest"]


def test_unknown_type_is_an_error():
    with pytest.raises(UnknownMessageType):
        HandlerRegistry().dispatch({"type": "nope"})


def test_inproc_routes_by_type():
    queue, ingest, hooks = InProcQueue(), [], []
    queue.register(WEBHOOK_DELIVERY, hooks.append)
    queue.start(ingest.append)
    try:
        queue.enqueue({"project_id": "p", "blob_key": "k"})
        queue.enqueue({"type": INGEST, "project_id": "p", "blob_key": "k2"})
        queue.enqueue({"type": WEBHOOK_DELIVERY, "delivery_id": "d1"})
        queue.drain()
    finally:
        queue.stop()
    assert [m["blob_key"] for m in ingest] == ["k", "k2"]
    assert hooks == [{"type": WEBHOOK_DELIVERY, "delivery_id": "d1"}]


def test_redis_queue_routes_by_type():
    pytest.importorskip("dramatiq")
    from dramatiq.brokers.stub import StubBroker

    from traceloom.adapters.full.redis_queue import RedisQueue

    queue = RedisQueue(broker=StubBroker())
    ingest, hooks = [], []
    queue.register(WEBHOOK_DELIVERY, hooks.append)
    queue.start(ingest.append)
    try:
        queue.enqueue({"blob_key": "k"})
        queue.enqueue({"type": WEBHOOK_DELIVERY, "delivery_id": "d1"})
        queue.broker.join(queue.queue_name, timeout=10_000)
        queue._worker.join()
    finally:
        queue.stop()
    assert ingest == [{"blob_key": "k"}]
    assert hooks == [{"type": WEBHOOK_DELIVERY, "delivery_id": "d1"}]


def test_sweeper_replays_due_deliveries(tmp_path):
    from traceloom.adapters.lite.local_blob import LocalBlobStore

    queue, hooks = InProcQueue(), []
    queue.register(WEBHOOK_DELIVERY, hooks.append)
    queue.start(lambda m: None)
    sweeper = Sweeper(
        LocalBlobStore(tmp_path),
        queue,
        min_age_seconds=0,
        interval_seconds=60,
        due_deliveries=lambda: [{"delivery_id": "d1"}, {"delivery_id": "d2"}],
    )
    try:
        assert sweeper.run_once() == 2
        queue.drain()
    finally:
        queue.stop()
    assert [m["delivery_id"] for m in hooks] == ["d1", "d2"]
    assert all(m["type"] == WEBHOOK_DELIVERY for m in hooks)
