import pytest

from traceloom.adapters.lite.memory import InMemoryCache, InMemoryRateLimiter, parse_limit


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_parse_limit():
    assert parse_limit("10/minute") == (10, 60)
    assert parse_limit("5/second") == (5, 1)
    with pytest.raises(ValueError):
        parse_limit("ten per minute")


def test_limiter_blocks_then_recovers():
    clock = Clock()
    limiter = InMemoryRateLimiter(clock)
    results = [limiter.hit("ip:a", "3/minute") for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[2].remaining == 0
    assert 0 < results[3].retry_after <= 60
    assert limiter.hit("ip:b", "3/minute").allowed  # keys are independent
    clock.now += 61
    assert limiter.hit("ip:a", "3/minute").allowed


def test_limiter_is_sliding_window():
    clock = Clock()
    limiter = InMemoryRateLimiter(clock)
    assert limiter.hit("k", "2/second").allowed
    clock.now += 0.6
    assert limiter.hit("k", "2/second").allowed
    assert not limiter.hit("k", "2/second").allowed
    clock.now += 0.5  # the first hit has left the window
    assert limiter.hit("k", "2/second").allowed


def test_cache_ttl_and_delete():
    clock = Clock()
    cache = InMemoryCache(clock)
    cache.set("a", 1, ttl_seconds=5)
    cache.set("b", 2)
    assert cache.get("a") == 1
    clock.now += 6
    assert cache.get("a") is None
    assert cache.get("b") == 2
    cache.delete("b")
    assert cache.get("b") is None
