"""Score analytics: pure-Python statistics over score values.

Every public function that summarises a sample returns ``NOT_ENOUGH_DATA`` when
fewer than ``MIN_SAMPLES`` values are given, so callers never show a statistic
computed from a handful of points.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

MIN_SAMPLES = 10
HISTOGRAM_BINS = 10

NOT_ENOUGH_DATA: dict[str, Any] = {"status": "not-enough-data", "min_samples": MIN_SAMPLES}


def _short(n: int) -> bool:
    return n < MIN_SAMPLES


def _not_enough(n: int) -> dict[str, Any]:
    return {**NOT_ENOUGH_DATA, "n": n}


def percentile(sorted_values: Sequence[float], p: float) -> float:
    """Linear-interpolated percentile (p in 0..100) of an ascending sequence."""
    if not sorted_values:
        raise ValueError("percentile of empty sequence")
    if not 0 <= p <= 100:
        raise ValueError("p must be within 0..100")
    pos = (len(sorted_values) - 1) * p / 100
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_values[lo])
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def summary(values: Sequence[float]) -> dict[str, Any]:
    """mean / median / p10 / p90 plus min, max and n."""
    n = len(values)
    if _short(n):
        return _not_enough(n)
    s = sorted(values)
    return {
        "status": "ok",
        "n": n,
        "mean": math.fsum(s) / n,
        "median": percentile(s, 50),
        "p10": percentile(s, 10),
        "p90": percentile(s, 90),
        "min": float(s[0]),
        "max": float(s[-1]),
    }


def histogram(values: Sequence[float], bins: int = HISTOGRAM_BINS) -> dict[str, Any]:
    """Equal-width histogram; the maximum value falls in the last bin."""
    n = len(values)
    if _short(n):
        return _not_enough(n)
    lo, hi = min(values), max(values)
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        idx = 0 if width == 0 else min(int((v - lo) / width), bins - 1)
        counts[idx] += 1
    edges = [lo + width * i for i in range(bins)] + [hi]
    return {
        "status": "ok",
        "n": n,
        "bins": [{"start": edges[i], "end": edges[i + 1], "count": counts[i]} for i in range(bins)],
    }


def bucket_start(ts: datetime, bucket: Literal["hour", "day"]) -> datetime:
    if bucket == "hour":
        return ts.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    raise ValueError("bucket must be 'hour' or 'day'")


def trend(
    points: Sequence[tuple[datetime, float]], bucket: Literal["hour", "day"] = "day"
) -> dict[str, Any]:
    """Mean score per hour/day bucket. Empty buckets between data are included (count 0)."""
    n = len(points)
    if _short(n):
        return _not_enough(n)
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    groups: dict[datetime, list[float]] = defaultdict(list)
    for ts, v in points:
        groups[bucket_start(ts, bucket)].append(v)
    cur, last = min(groups), max(groups)
    out = []
    while cur <= last:
        vals = groups.get(cur, [])
        out.append(
            {
                "start": cur,
                "count": len(vals),
                "mean": math.fsum(vals) / len(vals) if vals else None,
            }
        )
        cur += step
    return {"status": "ok", "n": n, "bucket": bucket, "points": out}


def pearson(xs: Sequence[float], ys: Sequence[float]) -> dict[str, Any]:
    """Pearson correlation of paired samples; ``r`` is None when a side is constant."""
    if len(xs) != len(ys):
        raise ValueError("xs and ys must have equal length")
    n = len(xs)
    if _short(n):
        return _not_enough(n)
    return {"status": "ok", "n": n, "r": _pearson(xs, ys)}


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    mx, my = math.fsum(xs) / n, math.fsum(ys) / n
    sxy = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    syy = math.fsum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def ranks(values: Sequence[float]) -> list[float]:
    """Average ranks (1-based); ties share the mean of their positions."""
    order = sorted(range(len(values)), key=values.__getitem__)
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def spearman(xs: Sequence[float], ys: Sequence[float]) -> dict[str, Any]:
    """Spearman rank correlation (Pearson over average ranks)."""
    if len(xs) != len(ys):
        raise ValueError("xs and ys must have equal length")
    n = len(xs)
    if _short(n):
        return _not_enough(n)
    return {"status": "ok", "n": n, "rho": _pearson(ranks(xs), ranks(ys))}


def confusion_matrix(
    a: Sequence[str], b: Sequence[str], labels: Sequence[str] | None = None
) -> dict[str, Any]:
    """Counts of (a label, b label) pairs; rows are ``a``, columns are ``b``."""
    if len(a) != len(b):
        raise ValueError("a and b must have equal length")
    n = len(a)
    if _short(n):
        return _not_enough(n)
    labs = list(labels) if labels is not None else sorted(set(a) | set(b))
    idx = {lab: i for i, lab in enumerate(labs)}
    matrix = [[0] * len(labs) for _ in labs]
    for x, y in zip(a, b, strict=True):
        matrix[idx[x]][idx[y]] += 1
    return {"status": "ok", "n": n, "labels": labs, "matrix": matrix}


def agreement(a: Sequence[str], b: Sequence[str]) -> dict[str, Any]:
    """Share of pairs where both raters gave the same label (0..100), plus Cohen's kappa."""
    if len(a) != len(b):
        raise ValueError("a and b must have equal length")
    n = len(a)
    if _short(n):
        return _not_enough(n)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[lab] * cb[lab] for lab in ca.keys() & cb.keys()) / (n * n)
    kappa = None if expected == 1 else (observed - expected) / (1 - expected)
    return {
        "status": "ok",
        "n": n,
        "agreement_pct": observed * 100,
        "kappa": kappa,
    }


def box_by_category(categories: Sequence[str], values: Sequence[float]) -> dict[str, Any]:
    """Five-number box summary of a numeric score for each category value."""
    if len(categories) != len(values):
        raise ValueError("categories and values must have equal length")
    n = len(values)
    if _short(n):
        return _not_enough(n)
    groups: dict[str, list[float]] = defaultdict(list)
    for c, v in zip(categories, values, strict=True):
        groups[c].append(v)
    boxes = {}
    for cat in sorted(groups):
        s = sorted(groups[cat])
        boxes[cat] = {
            "n": len(s),
            "min": float(s[0]),
            "q1": percentile(s, 25),
            "median": percentile(s, 50),
            "q3": percentile(s, 75),
            "max": float(s[-1]),
        }
    return {"status": "ok", "n": n, "boxes": boxes}
