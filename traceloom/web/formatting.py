"""Number and time formatting shared by the trace pages."""

from __future__ import annotations

import math
from datetime import UTC, datetime

# The finest amount a cost figure shows; anything smaller reads "< $0.000001".
MONEY_FLOOR_PLACES = 6


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
    """Dollars at `places` decimals. A real cost that would round to zero keeps two significant
    digits instead (0.0009 at 2 places is "$0.0009", not "$0.00")."""
    if value is None:
        return "–"
    if value and round(abs(value), places) == 0:
        digits = min(MONEY_FLOOR_PLACES, 1 - math.floor(math.log10(abs(value))))
        if round(abs(value), digits) == 0:
            return f"< ${10**-MONEY_FLOOR_PLACES:.{MONEY_FLOOR_PLACES}f}"
        return f"${value:.{digits}f}".rstrip("0")
    return f"${value:,.{places}f}"


def count(value: int | float | None) -> str:
    return "–" if value is None else f"{int(value):,}"


def plural(n: int | float | None, singular: str, plural_form: str | None = None) -> str:
    """The count with the noun that agrees with it: "1 trace", "2 traces", "1,500 traces"."""
    n = int(n or 0)
    return f"{n:,} {singular if n == 1 else plural_form or singular + 's'}"


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
    "plural": plural,
    "day": day,
    "clock": clock,
}
