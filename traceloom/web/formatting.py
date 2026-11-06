"""Number and time formatting shared by the trace pages."""

from __future__ import annotations

from datetime import UTC, datetime


def latency(ms: float | None) -> str:
    if ms is None:
        return "–"
    if ms < 1:
        return "0 ms"
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60_000:
        return f"{ms / 1000:.2f} s"
    minutes, seconds = divmod(ms / 1000, 60)
    return f"{int(minutes)} m {seconds:.0f} s"


def money(value: float | None, places: int = 4) -> str:
    if value is None:
        return "–"
    return f"${value:,.{places}f}"


def count(value: int | float | None) -> str:
    return "–" if value is None else f"{int(value):,}"


def day(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%b %-d, %Y") if value else ""


def clock(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%H:%M:%S") if value else ""


def percent(part: int, whole: int) -> str:
    if not whole:
        return "0%"
    share = part / whole * 100
    return f"{share:.1f}%" if share < 10 else f"{share:.0f}%"


FILTERS = {
    "latency": latency,
    "money": money,
    "count": count,
    "day": day,
    "clock": clock,
}
