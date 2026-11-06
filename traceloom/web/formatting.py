"""Number and time formatting shared by the data pages (registered as Jinja filters)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from traceloom.web.templating import templates


def fmt_duration(ms: float | None) -> str:
    if ms is None:
        return "-"
    if ms < 1000:
        return f"{ms:.0f} ms" if ms >= 1 else "<1 ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.2f} s" if seconds < 10 else f"{seconds:.1f} s"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min" if minutes % 60 else f"{minutes // 60} h"


def fmt_cost(value: float | None) -> str:
    if not value:
        return "$0.0000" if value == 0 else "-"
    return f"${value:,.4f}"


def fmt_int(value: Any) -> str:
    return f"{int(value or 0):,}"


def fmt_time(value: datetime | None, seconds: bool = False) -> str:
    """Time of day for today's values, date and time otherwise (UTC)."""
    if value is None:
        return ""
    value = value.astimezone(UTC)
    fmt = "%H:%M:%S" if seconds else "%H:%M"
    if value.date() == datetime.now(UTC).date():
        return value.strftime(fmt)
    return value.strftime("%b %-d, " + fmt)


def fmt_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


for _name, _fn in (
    ("duration", fmt_duration),
    ("cost", fmt_cost),
    ("intcomma", fmt_int),
    ("clock", fmt_time),
    ("percent", fmt_percent),
):
    templates.env.filters[_name] = _fn
