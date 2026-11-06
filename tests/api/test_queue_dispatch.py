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


def test_inproc_lane_runs_beside_ingest_and_drain_waits_for_it():
    import threading

    queue, ingest, hooks = InProcQueue(), [], []
    release, entered = threading.Event(), threading.Event()

    def slow_hook(message):
        entered.set()
        release.wait(5)
        hooks.append(message["delivery_id"])

    queue.register(WEBHOOK_DELIVERY, slow_hook, workers=1, capacity=10)
    queue.start(ingest.append)
    try:
        queue.enqueue({"type": WEBHOOK_DELIVERY, "delivery_id": "d1"})
        assert entered.wait(5)
        queue.enqueue({"project_id": "p", "blob_key": "k"})
        for _ in range(200):  # ingest is handled while the webhook is still stuck
            if ingest:
                break
            threading.Event().wait(0.01)
        assert [m["blob_key"] for m in ingest] == ["k"] and hooks == []
        assert queue.pending == 1
        threading.Timer(0.1, release.set).start()
        queue.drain()  # also waits for the lane
        assert hooks == ["d1"] and queue.pending == 0
    finally:
        release.set()
        queue.stop()


def test_inproc_lane_is_bounded_and_skips_duplicates():
    queue, hooks = InProcQueue(), []
    queue.register(WEBHOOK_DELIVERY, hooks.append, workers=1, capacity=2)
    queue.start(lambda m: None)
    queue.pause()
    try:
        for i in (1, 1, 2, 3):  # d1 twice (a sweep re-sent it), then one more than fits
            queue.enqueue({"type": WEBHOOK_DELIVERY, "delivery_id": f"d{i}"})
        assert queue.pending == 2
        queue.resume()
        queue.drain()
    finally:
        queue.stop()
    assert [m["delivery_id"] for m in hooks] == ["d1", "d2"]


def test_full_mode_delivery_pool_runs_off_the_consumer_and_never_blocks_it():
    import threading
    import time

    from traceloom.webhooks.deliver import DeliveryPool

    release, done = threading.Event(), []

    def slow(message):
        release.wait(5)
        done.append(message["delivery_id"])

    pool = DeliveryPool(slow, workers=1, backlog=2)
    try:
        started = time.monotonic()
        for i in range(10):  # one in flight, two waiting, the rest dropped for the sweeper
            pool.submit({"type": WEBHOOK_DELIVERY, "delivery_id": f"d{i}"})
        assert time.monotonic() - started < 0.5
        release.set()
        pool.join()
        assert 1 <= len(done) <= 3 and done[0] == "d0"
    finally:
        release.set()
        pool.stop()
