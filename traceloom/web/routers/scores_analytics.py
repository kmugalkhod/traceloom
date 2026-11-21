"""Evaluation > Scores: distributions, a trend split by source, and a compare panel (Viewer+)."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from traceloom.auth.deps import Access, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.models import Score
from traceloom.db.session import get_db
from traceloom.scores import analytics
from traceloom.web import pagefmt  # noqa: F401  (registers the filters)
from traceloom.web.context import env_context, page_context
from traceloom.web.envpref import resolve_env, saved_env
from traceloom.web.ranges import range_urls, resolve_range
from traceloom.web.templating import render

router = APIRouter(tags=["scores-analytics"])

SOURCES = {"annotation": "ANNOTATION", "eval": "EVAL", "api": "API"}
SOURCE_LABELS = {"ANNOTATION": "Annotation", "EVAL": "Eval", "API": "API"}
SOURCE_ORDER = ("ANNOTATION", "EVAL", "API")
DEFAULT_RANGE = "7d"
MAX_ROWS = 50_000
MAX_CARDS = 12
MAX_POINTS = 2000
FEEDBACK = "public_feedback"
KIND_LABEL = {"NUMERIC": "numeric", "BOOLEAN": "boolean", "CATEGORICAL": "categorical"}


def _fmt(v: float | None, digits: int = 2) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}"


def _label(row: Score) -> str:
    if row.data_type == "BOOLEAN":
        return "true" if (row.value or 0) >= 0.5 else "false"
    return row.string_value or ""


def _load(
    db: Session, project_id: str, start: datetime, end: datetime, env: str | None, feedback: bool
) -> list[Score]:
    stmt = select(Score).where(
        Score.project_id == project_id, Score.timestamp >= start, Score.timestamp <= end
    )
    if env:
        stmt = stmt.where(Score.environment == env)
    if not feedback:
        stmt = stmt.where(or_(Score.channel.is_(None), Score.channel != FEEDBACK))
    return list(db.scalars(stmt.order_by(Score.timestamp).limit(MAX_ROWS)))


def _tick(v: float) -> str:
    """Axis tick: at most two decimals, trailing zeros dropped (0, 0.5, 1)."""
    out = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if out in ("", "-0") else out


def _hist_bars(counts: list[int]) -> list[dict[str, Any]]:
    """Contiguous histogram bars (viewBox 300 x 120) with a 4-unit gap between them."""
    top = max(counts, default=0) or 1
    slot = 300 / max(len(counts), 1)
    out = []
    for i, count in enumerate(counts):
        h = count / top * 112
        out.append(
            {
                "x": round(slot * i + 2, 1),
                "w": round(slot - 4, 1),
                "y": round(120 - h, 1),
                "h": round(h, 1),
                "series": 1,
            }
        )
    return out


def _bars(items: list[tuple[str, int]], series: list[int] | None = None) -> list[dict[str, Any]]:
    """SVG bar geometry (viewBox 300 x 120) for label/count pairs."""
    top = max((c for _, c in items), default=0) or 1
    slot = 300 / max(len(items), 1)
    width = min(90.0, slot * 0.7)
    out = []
    for i, (label, count) in enumerate(items):
        h = count / top * 110
        out.append(
            {
                "label": label,
                "count": count,
                "x": round(slot * i + (slot - width) / 2, 1),
                "w": round(width, 1),
                "y": round(120 - h, 1),
                "h": round(h, 1),
                "series": series[i % len(series)] if series else i % 6 + 1,
            }
        )
    return out


def _card(name: str, rows: list[Score]) -> dict[str, Any]:
    kind = rows[0].data_type
    n = len(rows)
    card: dict[str, Any] = {"name": name, "kind": KIND_LABEL.get(kind, kind.lower()), "n": n}
    if kind == "NUMERIC":
        values = [r.value for r in rows if r.value is not None]
        hist = analytics.histogram(values)
        card["type"] = "numeric"
        if hist["status"] != "ok":
            card["enough"] = False
            return card
        stats = analytics.summary(values)
        card.update(
            enough=True,
            stats=[
                ("Mean", _fmt(stats["mean"])),
                ("Median", _fmt(stats["median"])),
                ("p10", _fmt(stats["p10"])),
                ("p90", _fmt(stats["p90"])),
                ("n", f"{n:,}"),
            ],
        )
        discrete = analytics.discrete_histogram(values)
        if discrete is not None:
            dbins = discrete["bins"]
            every = math.ceil(len(dbins) / 10)
            top_bin = max(dbins, key=lambda b: b["count"])
            card.update(
                bars=_hist_bars([b["count"] for b in dbins]),
                ticks=[str(b["value"]) if i % every == 0 else "" for i, b in enumerate(dbins)],
                slots=True,
                table=[(str(b["value"]), b["count"]) for b in dbins],
                table_head=("Value", "Count"),
                aria=(
                    f"Histogram of {name}, one bar per value from {dbins[0]['value']} to "
                    f"{dbins[-1]['value']}; most common {top_bin['value']} "
                    f"({top_bin['count']:,} scores)"
                ),
            )
            return card
        bins = hist["bins"]
        card.update(
            bars=_hist_bars([b["count"] for b in bins]),
            ticks=[
                _tick(stats["min"]),
                _tick((stats["min"] + stats["max"]) / 2),
                _tick(stats["max"]),
            ],
            slots=False,
            table=[(f"{_fmt(b['start'])} to {_fmt(b['end'])}", b["count"]) for b in bins],
            table_head=("Bin", "Count"),
            aria=(
                f"Histogram of {name} in 10 bins from {_fmt(stats['min'])} to {_fmt(stats['max'])}"
            ),
        )
        return card
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[_label(r)] += 1
    if kind == "BOOLEAN":
        items = [("true", counts.get("true", 0)), ("false", counts.get("false", 0))]
    else:
        items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    card.update(
        type="boolean" if kind == "BOOLEAN" else "categorical",
        enough=n >= analytics.MIN_SAMPLES,
        counts=items,
        table=items,
        table_head=("Value", "Count"),
    )
    if kind == "BOOLEAN":
        card["stats"] = [
            ("True", f"{items[0][1] / n * 100:.0f}%"),
            ("False", f"{items[1][1] / n * 100:.0f}%"),
            ("n", f"{n:,}"),
        ]
    else:
        card["stats"] = [(k, f"{c:,}") for k, c in items[:5]]
    if card["enough"]:
        # Booleans use teal (true) and amber (false), as in the approved design.
        palette = [2, 4] if kind == "BOOLEAN" else None
        card["bars"] = _bars([(f"{k} · {c}", c) for k, c in items], palette)
        card["aria"] = f"Bar chart for {name}: " + ", ".join(f"{k} {c}" for k, c in items)
    return card


def _trend(
    rows: list[Score], hourly: bool, window: tuple[datetime, datetime] | None = None
) -> dict[str, Any] | None:
    """Mean per bucket for each source with enough points; None when none qualifies.

    With a window, the axis has every bucket in it, so data on one day still draws a
    week-wide axis instead of a single label.
    """
    by_source: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for r in rows:
        if r.value is not None:
            by_source[r.source].append((r.timestamp, r.value))
    bucket = "hour" if hourly else "day"
    series, starts = [], set()
    for source in SOURCE_ORDER:
        result = analytics.trend(by_source.get(source, []), bucket)
        if result["status"] != "ok":
            continue
        series.append((source, result))
        starts.update(p["start"] for p in result["points"])
    if not series:
        return None
    if window:
        step = timedelta(hours=1) if hourly else timedelta(days=1)
        cur, last = (analytics.bucket_start(t, bucket) for t in window)
        while cur <= last:
            starts.add(cur)
            cur += step
    axis = sorted(starts)
    cols = []
    for source, result in series:
        lookup = {p["start"]: p for p in result["points"]}
        cols.append(
            {
                "source": source,
                "name": SOURCE_LABELS[source],
                "values": [None if lookup.get(s) is None else lookup[s]["mean"] for s in axis],
                "counts": [0 if lookup.get(s) is None else lookup[s]["count"] for s in axis],
            }
        )
    fmt = "%b %-d %H:00" if hourly else "%b %-d"
    return {
        "labels": [s.strftime("%Y-%m-%dT%H:%M:%SZ") for s in axis],
        "series": cols,
        "table": [
            {
                "label": s.strftime(fmt),
                "cells": [
                    (_fmt(c["values"][i]) if c["values"][i] is not None else "n/a", c["counts"][i])
                    for c in cols
                ],
            }
            for i, s in reversed(list(enumerate(axis)))
        ],
    }


def _latest(rows: list[Score], name: str, source: str) -> dict[str, Score]:
    """Latest score per trace for one (name, source)."""
    out: dict[str, Score] = {}
    for r in rows:  # rows are ordered by timestamp
        if r.name == name and r.source == source and r.trace_id:
            out[r.trace_id] = r
    return out


def _kappa_word(k: float | None) -> str:
    if k is None:
        return "undefined"
    for limit, word in ((0, "worse than chance"), (0.2, "slight"), (0.4, "fair"),
                        (0.6, "moderate"), (0.8, "substantial")):  # fmt: skip
        if k < limit:
            return word
    return "almost perfect"


def _compare(
    rows: list[Score], a: tuple[str, str], b: tuple[str, str], feedback: bool
) -> dict[str, Any]:
    left, right = _latest(rows, *a), _latest(rows, *b)
    ids = sorted(left.keys() & right.keys())
    pairs = [(left[i], right[i]) for i in ids]
    n = len(pairs)
    out: dict[str, Any] = {"n": n, "a": a, "b": b, "feedback": feedback, "kind": None}
    if n < analytics.MIN_SAMPLES:
        out["enough"] = False
        return out
    out["enough"] = True
    ka, kb = pairs[0][0].data_type, pairs[0][1].data_type
    num_a, num_b = ka == "NUMERIC", kb == "NUMERIC"
    if num_a and num_b:
        xs = [p[0].value for p in pairs if p[0].value is not None and p[1].value is not None]
        ys = [p[1].value for p in pairs if p[0].value is not None and p[1].value is not None]
        r, rho = analytics.pearson(xs, ys), analytics.spearman(xs, ys)
        step = math.ceil(len(xs) / MAX_POINTS)
        shown = list(zip(xs, ys, strict=True))[::step]
        out.update(
            kind="scatter",
            tiles=[
                ("Pearson r", _fmt(r.get("r")), ""),
                ("Spearman ρ", _fmt(rho.get("rho")), ""),
                ("n", f"{len(xs):,}", "matched traces"),
                (
                    "Shown",
                    f"{len(shown):,}",
                    f"of {len(xs):,}" + (" · Sampled" if step > 1 else ""),
                ),
            ],
            chart={"x": [p[0] for p in shown], "y": [p[1] for p in shown]},
            aria=(
                f"Scatter of {a[0]} ({SOURCE_LABELS[a[1]]}) against {b[0]} "
                f"({SOURCE_LABELS[b[1]]}), {len(shown)} points, r {_fmt(r.get('r'))}"
            ),
            table=[(_fmt(x), _fmt(y)) for x, y in shown[:20]],
            more=max(0, len(shown) - 20),
        )
    elif not num_a and not num_b:
        la, lb = [_label(p[0]) for p in pairs], [_label(p[1]) for p in pairs]
        labels = ["true", "false"] if ka == kb == "BOOLEAN" else None
        cm = analytics.confusion_matrix(la, lb, labels)
        ag = analytics.agreement(la, lb)
        labs = cm["labels"]
        cells = [
            [
                {
                    "count": c,
                    "pct": f"{c / n * 100:.0f}%",
                    "w": round(c / n * 100),
                    "agree": labs[i] == labs[j],
                }
                for j, c in enumerate(row)
            ]
            for i, row in enumerate(cm["matrix"])
        ]
        agreed = round(ag["agreement_pct"] * n / 100)
        out.update(
            kind="matrix",
            labels=labs,
            cells=cells,
            tiles=[
                ("Agreement", f"{ag['agreement_pct']:.0f}%", f"{agreed} of {n} match"),
                ("Cohen's κ", _fmt(ag["kappa"]), _kappa_word(ag["kappa"])),
                ("n matched", str(n), "traces"),
                ("Feedback", "Included" if feedback else "Excluded", "toggle above"),
            ],
            table=[
                (labs[i], labs[j], cells[i][j]["count"])
                for i in range(len(labs))
                for j in range(len(labs))
            ],
        )
    else:
        cat_first = not num_a
        cats = [_label(p[0] if cat_first else p[1]) for p in pairs]
        vals = [(p[1] if cat_first else p[0]).value or 0.0 for p in pairs]
        box = analytics.box_by_category(cats, vals)
        lo = min(b["min"] for b in box["boxes"].values())
        hi = max(b["max"] for b in box["boxes"].values())
        span = (hi - lo) or 1

        def y(v: float) -> float:
            return round(112 - (v - lo) / span * 104, 1)

        slot = 300 / len(box["boxes"])
        boxes = []
        for i, (cat, bx) in enumerate(box["boxes"].items()):
            cx = slot * i + slot / 2
            boxes.append(
                {
                    "cat": cat, "n": bx["n"], "median": _fmt(bx["median"]),
                    "x": round(cx - 25, 1), "x2": round(cx + 25, 1), "cx": round(cx, 1),
                    "q3": y(bx["q3"]), "q1": y(bx["q1"]), "med": y(bx["median"]),
                    "top": y(bx["max"]), "bot": y(bx["min"]),
                    "h": round(y(bx["q1"]) - y(bx["q3"]), 1),
                }
            )  # fmt: skip
        out.update(
            kind="box",
            boxes=boxes,
            table=[(b["cat"], b["median"], b["n"]) for b in boxes],
            tiles=[
                ("n matched", str(n), "traces"),
                ("Categories", str(len(boxes)), ""),
                ("Feedback", "Included" if feedback else "Excluded", "toggle above"),
            ],
            aria="Box plot of "
            + (b[0] if cat_first else a[0])
            + " per "
            + (a[0] if cat_first else b[0]),
        )
    return out


def _pick_source(raw: str, default: str) -> str:
    return raw.upper() if raw.upper() in SOURCE_LABELS else default


def build_view(
    rows: list[Score],
    params: Any,
    hourly: bool,
    feedback: bool,
    window: tuple[datetime, datetime] | None = None,
) -> dict[str, Any]:
    by_name: dict[str, list[Score]] = defaultdict(list)
    for r in rows:
        by_name[r.name].append(r)
    names = sorted(by_name)
    selected = params.get("score", "")
    if selected not in by_name:
        selected = ""
    source_key = params.get("source", "all")
    source = SOURCES.get(source_key)
    shown = {n: [r for r in by_name[n] if not source or r.source == source] for n in names}
    cards = [_card(n, rs) for n, rs in shown.items() if rs and (not selected or n == selected)]

    trend_name = selected or next(
        (c["name"] for c in cards if c["type"] in ("numeric", "boolean")), ""
    )
    trend = None
    if trend_name:
        trend_rows = shown.get(trend_name, [])
        if trend_rows and trend_rows[0].data_type in ("NUMERIC", "BOOLEAN"):
            trend = _trend(trend_rows, hourly, window)

    # Compare. The preset (annotation vs eval on the same score) is the default.
    custom = bool(params.get("a") or params.get("b")) and params.get("preset") != "human-judge"
    if custom:
        a = (
            params.get("a") or (names[0] if names else ""),
            _pick_source(params.get("as", ""), "ANNOTATION"),
        )
        b = (params.get("b") or a[0], _pick_source(params.get("bs", ""), "EVAL"))
    else:
        both = next(
            (n for n in names if {"ANNOTATION", "EVAL"} <= {r.source for r in by_name[n]}),
            None,
        )
        name = selected or both or (names[0] if names else "")
        a, b = (name, "ANNOTATION"), (name, "EVAL")
    compare = _compare(rows, a, b, feedback) if names else None
    return {
        "names": [{"name": n, "count": len(by_name[n])} for n in names],
        "selected": selected,
        "source_key": source_key if source else "all",
        "cards": cards[:MAX_CARDS],
        "more_cards": max(0, len(cards) - MAX_CARDS),
        "trend": trend,
        "trend_name": trend_name,
        "compare": compare,
        "preset": not custom,
        "a": a,
        "b": b,
        "total": len(rows),
    }


@router.get("/project/{project_id}/scores/analytics", response_class=HTMLResponse)
def analytics_page(
    request: Request,
    access: Access = Depends(require(Perm.EVAL_READ)),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
):
    params = request.query_params
    range_key, start, end = resolve_range(params, default=DEFAULT_RANGE)
    env = resolve_env(params, saved_env(db, access.user.id, access.project.id))
    feedback = params.get("feedback", "1") != "0"
    rows = _load(db, access.project.id, start, end, env, feedback)
    view = build_view(rows, params, range_key in ("1h", "24h"), feedback, (start, end))
    path = request.url.path
    keep = {k: v for k, v in params.items() if k != "preset"}

    def link(**over: str) -> str:
        from urllib.parse import urlencode

        merged = {**keep, **over}
        merged = {k: v for k, v in merged.items() if v not in ("", None)}
        return f"{path}?{urlencode(merged)}" if merged else path

    context = page_context(
        db,
        access,
        active="scores",
        view=view,
        range_key=range_key,
        range_urls=range_urls(path, params),
        feedback=feedback,
        env_param=params.get("env", ""),
        source_urls={k: link(source="" if k == "all" else k) for k in ("all", *SOURCES)},
        preset_url=link(preset="human-judge", a="", **{"as": ""}, b="", bs=""),
        source_options=list(SOURCE_LABELS.items()),
        source_label=SOURCE_LABELS,
        **env_context(db, container.trace_store, access.user, access.project.id, env),
    )
    return render(request, "scores/analytics.html", context)
