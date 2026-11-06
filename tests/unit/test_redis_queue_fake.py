from __future__ import annotations

import dramatiq
import fakeredis
import pytest
from dramatiq.brokers.stub import StubBroker

from traceloom.adapters.full.redis_limiter import RedisCache, RedisRateLimiter
from traceloom.adapters.full.redis_queue import RedisQueue


@pytest.fixture
def broker():
    broker = StubBroker()
    broker.emit_after("process_boot")
    dramatiq.set_broker(broker)
    yield broker
    broker.flush_all()
    broker.close()


@pytest.fixture
def queue(broker):
    q = RedisQueue(broker=broker, worker_threads=2, max_retries=2)
    yield q
    q.stop()


def drain(broker, queue):
    broker.join(queue.queue_name, fail_fast=False, timeout=10_000)
    queue._worker.join()


def test_enqueue_start_delivers_each_message_to_the_handler(broker, queue):
    seen = []
    queue.start(seen.append)
    for i in range(5):
        queue.enqueue({"project_id": "p", "blob_key": f"pending/p/{i}.json"})
    drain(broker, queue)
    assert sorted(m["blob_key"] for m in seen) == [f"pending/p/{i}.json" for i in range(5)]
    assert all(set(m) == {"project_id", "blob_key"} for m in seen)


def test_messages_wait_in_the_queue_until_a_consumer_starts(broker, queue):
    queue.enqueue({"project_id": "p", "blob_key": "k"})
    seen = []
    queue.start(seen.append)
    drain(broker, queue)
    assert len(seen) == 1


def test_the_actor_is_named_process_batch(queue):
    assert queue.process_batch.actor_name == "process_batch"
    assert queue.process_batch.queue_name == "traceloom-ingest"


def test_failed_handler_is_retried_then_gives_up(broker, queue):
    calls = []

    def flaky(message):
        calls.append(message)
        raise RuntimeError("boom")

    queue.start(flaky)
    queue.process_batch.options["min_backoff"] = 1
    queue.process_batch.options["max_backoff"] = 1
    queue.enqueue({"project_id": "p", "blob_key": "k"})
    drain(broker, queue)
    assert len(calls) == 3  # first try plus max_retries=2
    assert broker.dead_letters  # the sweeper replays the pending blob from here


def test_start_and_stop_are_idempotent(queue):
    queue.start(lambda m: None)
    queue.start(lambda m: None)
    queue.stop()
    queue.stop()


def test_ping_is_true_for_a_stub_and_uses_the_client_for_redis(broker, queue):
    assert queue.ping() is True
    queue.broker = type("B", (), {"client": fakeredis.FakeRedis()})()
    assert queue.ping() is True


# --- limiter and cache on fakeredis ---


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_rate_limiter_allows_up_to_the_limit_then_denies(redis_client):
    clock = Clock()
    limiter = RedisRateLimiter(redis_client, clock=clock)
    results = [limiter.hit("k", "3/minute") for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0]
    assert 0 < results[3].retry_after <= 60


def test_rate_limiter_window_slides_and_keys_are_independent(redis_client):
    clock = Clock()
    limiter = RedisRateLimiter(redis_client, clock=clock)
    for _ in range(2):
        assert limiter.hit("a", "2/second").allowed
    assert not limiter.hit("a", "2/second").allowed
    assert limiter.hit("b", "2/second").allowed
    clock.now += 1.5
    assert limiter.hit("a", "2/second").allowed


def test_denied_hits_do_not_extend_the_window(redis_client):
    clock = Clock()
    limiter = RedisRateLimiter(redis_client, clock=clock)
    limiter.hit("k", "1/minute")
    for _ in range(10):
        assert not limiter.hit("k", "1/minute").allowed
    clock.now += 61
    assert limiter.hit("k", "1/minute").allowed


def test_rate_limiter_rejects_bad_limit_strings(redis_client):
    with pytest.raises(ValueError):
        RedisRateLimiter(redis_client).hit("k", "lots")


def test_cache_roundtrip_ttl_and_delete(redis_client):
    cache = RedisCache(redis_client)
    assert cache.get("missing") is None
    cache.set("k", {"a": [1, 2]})
    assert cache.get("k") == {"a": [1, 2]}
    cache.set("t", "x", ttl_seconds=30)
    assert 0 < redis_client.pttl("traceloom:cache:t") <= 30_000
    cache.delete("k")
    assert cache.get("k") is None
