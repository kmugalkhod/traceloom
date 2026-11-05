"""In-process RateLimiter and Cache (lite mode and tests)."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from traceloom.ports import RateResult

_UNITS = {"second": 1, "minute": 60, "hour": 3600}


def parse_limit(limit: str) -> tuple[int, int]:
    """'10/minute' -> (10, 60)."""
    try:
        count, _, unit = limit.partition("/")
        return int(count), _UNITS[unit.strip().lower().rstrip("s")]
    except (ValueError, KeyError) as exc:
        raise ValueError(
            f"invalid rate limit {limit!r}; expected <count>/<second|minute|hour>"
        ) from exc


class InMemoryRateLimiter:
    """Sliding-window limiter, safe across threads."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}
        self._calls = 0

    def hit(self, key: str, limit: str) -> RateResult:
        count, window = parse_limit(limit)
        now = self._clock()
        with self._lock:
            self._calls += 1
            if self._calls % 1024 == 0:  # drop idle keys so the map cannot grow without bound
                horizon = now - max(_UNITS.values())
                self._hits = {k: h for k, h in self._hits.items() if h and h[-1] > horizon}
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= count:
                return RateResult(False, 0, retry_after=hits[0] + window - now)
            hits.append(now)
            remaining = count - len(hits)
            return RateResult(True, remaining)

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


class InMemoryCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float | None, Any]] = {}

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires, value = entry
            if expires is not None and expires <= self._clock():
                del self._data[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl_seconds: float | None = None) -> None:
        expires = None if ttl_seconds is None else self._clock() + ttl_seconds
        with self._lock:
            self._data[key] = (expires, value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
