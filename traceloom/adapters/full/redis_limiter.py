"""Redis RateLimiter (sliding window) and Cache (JSON values with TTL)."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import Any

from traceloom.adapters.lite.memory import parse_limit
from traceloom.ports import RateResult

PREFIX = "traceloom:"


class RedisRateLimiter:
    """Sliding window over a sorted set per (key, limit). Each hit is added and counted in one
    MULTI/EXEC; a hit over the limit is removed again so denied calls do not extend the window."""

    def __init__(self, client: Any, clock: Callable[[], float] = time.time) -> None:
        self._redis = client
        self._clock = clock

    def hit(self, key: str, limit: str) -> RateResult:
        count, window = parse_limit(limit)
        name = f"{PREFIX}rl:{limit}:{key}"
        now = self._clock()
        member = f"{now}:{uuid.uuid4().hex}"
        pipe = self._redis.pipeline(transaction=True)
        pipe.zremrangebyscore(name, 0, now - window)
        pipe.zadd(name, {member: now})
        pipe.zcard(name)
        pipe.expire(name, window + 1)
        _, _, used, _ = pipe.execute()
        if used <= count:
            return RateResult(True, count - used)
        self._redis.zrem(name, member)
        oldest = self._redis.zrange(name, 0, 0, withscores=True)
        retry = (oldest[0][1] + window - now) if oldest else float(window)
        return RateResult(False, 0, retry_after=max(retry, 0.0))


class RedisCache:
    def __init__(self, client: Any) -> None:
        self._redis = client

    def get(self, key: str) -> Any | None:
        raw = self._redis.get(PREFIX + "cache:" + key)
        return None if raw is None else json.loads(raw)

    def set(self, key: str, value: Any, ttl_seconds: float | None = None) -> None:
        raw = json.dumps(value, default=str)
        name = PREFIX + "cache:" + key
        if ttl_seconds is None:
            self._redis.set(name, raw)
        else:
            self._redis.set(name, raw, px=max(1, int(ttl_seconds * 1000)))

    def delete(self, key: str) -> None:
        self._redis.delete(PREFIX + "cache:" + key)
