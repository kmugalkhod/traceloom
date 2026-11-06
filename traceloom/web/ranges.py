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
    params: Mapping[str, str], now: datetime | None = None
) -> tuple[str, datetime, datetime]:
    """(key, from, to) for `range`; unknown values fall back to the default."""
    key = params.get("range", DEFAULT_RANGE)
    if key not in RANGES:
        key = DEFAULT_RANGE
    end = (now or datetime.now(UTC)).astimezone(UTC)
    return key, end - RANGES[key], end


def environment(params: Mapping[str, str]) -> str | None:
    value = (params.get("env") or params.get("environment") or "").strip()
    return value or None


def range_links(path: str, params: Mapping[str, str], current: str) -> list[dict[str, object]]:
    """Links for the date-range segmented control, keeping every other query parameter."""
    from urllib.parse import urlencode

    links = []
    for key in RANGES:
        query = {k: v for k, v in params.items() if k != "range"}
        query["range"] = key
        links.append({"key": key, "href": f"{path}?{urlencode(query)}", "current": key == current})
    return links
