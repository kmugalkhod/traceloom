"""The date range and environment a page is showing, read from `range=` and `env=`."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

RANGES: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
DEFAULT_RANGE = "24h"


def resolve_range(
    params: Mapping[str, str], now: datetime | None = None, default: str = DEFAULT_RANGE
) -> tuple[str, datetime, datetime]:
    """(key, from, to) for `range`; unknown values fall back to `default`."""
    key = params.get("range", default)
    if key not in RANGES:
        key = default
    end = (now or datetime.now(UTC)).astimezone(UTC)
    return key, end - RANGES[key], end


def environment(params: Mapping[str, str]) -> str | None:
    value = (params.get("env") or params.get("environment") or "").strip()
    return value or None
