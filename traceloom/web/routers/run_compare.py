"""Run comparison page: up to four runs of one dataset side by side (Viewer+)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from traceloom.auth.deps import Access, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.datasets import compare
from traceloom.datasets import runs as dataset_runs
from traceloom.datasets.compare import Cell, ItemRow, RunAggregate
from traceloom.datasets.service import DatasetError, DatasetNotFound
from traceloom.db.session import get_db
from traceloom.web import pagefmt
from traceloom.web.context import page_context
from traceloom.web.routers.datasets import base_url, compact, load_dataset
from traceloom.web.templating import render

router = APIRouter(tags=["run-compare"])

LETTERS = "abcd"
LONG_TEXT = 140  # characters from which a cell offers "Show more"
SORTS = ("item", "delta")


@dataclass
class MetricCell:
    text: str
    n: int | None = None
    best: bool = False
    delta: str = ""
    up: bool = True  # the delta colour: green when it is an improvement


def _score_text(value: float, data_type: str) -> str:
    if data_type == compare.BOOLEAN:
        return "true" if value > 0 else "false"
    return f"{value:.2f}"


def _best_flags(values: list[float | None], *, high: bool) -> list[bool]:
    """Flag the best value; nothing is best when every run is equal or only one has a value."""
    present = [v for v in values if v is not None]
    if len(present) < 2 or len(set(present)) == 1:
        return [False] * len(values)
    top = max(present) if high else min(present)
    return [v == top for v in values]


def _metric_rows(aggs: list[RunAggregate], names: list[str]) -> list[dict[str, Any]]:
    base = aggs[0]
    rows: list[dict[str, Any]] = []
    for name in names:
        cells: list[MetricCell | None] = []
        for i, agg in enumerate(aggs):
            stat = agg.scores.get(name)
            if stat is None:
                cells.append(None)
                continue
            delta, up = "", True
            if i and name in base.scores:
                diff = stat.mean - base.scores[name].mean
                delta, up = f"{diff:+.2f}", diff >= 0
            cells.append(MetricCell(f"{stat.mean:.2f}", stat.n, stat.best, delta, up))
        rows.append({"label": name, "sub": "mean (n)", "cells": cells})

    def other(label: str, sub: str, get, show, *, high: bool, relative: bool = False) -> None:
        values = [get(a) for a in aggs]
        flags = _best_flags(values, high=high)
        cells: list[MetricCell | None] = []
        for i, (agg, value) in enumerate(zip(aggs, values, strict=True)):
            if value is None:
                cells.append(None)
                continue
            delta, up = "", True
            if relative and i and values[0]:
                change = (value - values[0]) / values[0] * 100
                if round(change) != 0:
                    delta, up = f"{change:+.0f}%", change <= 0
            cells.append(MetricCell(show(agg), None, flags[i], delta, up))
        rows.append({"label": label, "sub": sub, "cells": cells})

    other(
        "Success rate", "", lambda a: a.success_rate,
        lambda a: f"{a.success_rate:.0%}", high=True,
    )  # fmt: skip
    other(
        "Latency", "mean · p95", lambda a: a.mean_latency_ms,
        lambda a: f"{pagefmt.fmt_duration(a.mean_latency_ms)} · "
        f"{pagefmt.fmt_duration(a.p95_latency_ms)}", high=False,
    )  # fmt: skip
    other(
        "Total cost", "", lambda a: a.total_cost, lambda a: pagefmt.fmt_cost(a.total_cost),
        high=False, relative=True,
    )  # fmt: skip
    return rows


def _item_cell(cell: Cell | None, row: ItemRow, run_id: str, base_cell: Cell | None) -> dict:
    if cell is None:
        return {"state": "none"}
    if cell.status == "failed" or cell.error:
        return {"state": "failed", "error": cell.error or "Failed"}
    output = compact(cell.output)
    deltas: list[tuple[str, bool]] = []
    for name, diff in sorted(row.deltas.get(run_id, {}).items()):
        before = base_cell.scores.get(name) if base_cell else None
        if before is not None and before.data_type == compare.BOOLEAN:
            if diff:
                deltas.append(("true to false" if diff < 0 else "false to true", diff < 0))
        elif diff:
            deltas.append((f"{diff:+.2f}", diff < 0))
    return {
        "state": "ok",
        "output": output,
        "long": len(output) > LONG_TEXT,
        "chips": [
            {"name": n, "value": _score_text(s.value, s.data_type)}
            for n, s in sorted(cell.scores.items())
        ],
        "deltas": deltas,
        "reg": run_id in row.regressed_in,
        "latency": pagefmt.fmt_duration(cell.latency_ms) if cell.latency_ms is not None else "",
        "cost": pagefmt.fmt_cost(cell.cost) if cell.cost is not None else "",
    }


def _parse_runs(request: Request) -> list[str]:
    ids: list[str] = []
    for raw in request.query_params.getlist("runs"):
        ids += [part.strip() for part in raw.split(",") if part.strip()]
    return list(dict.fromkeys(ids))


def _threshold(raw: str) -> float:
    try:
        value = float(raw.strip() or 0)
    except ValueError:
        return 0.0
    return max(value, 0.0) if math.isfinite(value) else 0.0


def _int(raw: str, default: int) -> int:
    try:
        return int(raw)
    except ValueError:
        return default


@router.get("/project/{project_id}/datasets/{dataset_id}/compare", response_class=HTMLResponse)
def compare_page(
    request: Request,
    dataset_id: str,
    access: Access = Depends(require(Perm.EVAL_READ)),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
):
    project = access.project
    dataset = load_dataset(db, access, dataset_id)
    params = request.query_params
    requested = _parse_runs(request)
    too_many = len(requested) > compare.MAX_RUNS
    run_ids = requested[: compare.MAX_RUNS]
    only_regressed = params.get("regressed") == "1"
    raw_threshold = params.get("threshold", "").strip()
    threshold = _threshold(raw_threshold)
    sort = params.get("sort") if params.get("sort") in SORTS else "item"
    sort_score = params.get("score", "")
    page = _int(params.get("page", "1"), 1)

    base = f"{base_url(project.id)}/{dataset.id}"
    all_runs = dataset_runs.list_runs(db, project, dataset)
    error = ""
    result = None
    if run_ids:
        options: dict[str, Any] = {
            "threshold": threshold,
            "only_regressed": only_regressed,
            "page": page,
        }
        try:
            if sort == "delta":
                if not sort_score:  # default to the first score of the compared runs
                    probe = compare.compare_runs(
                        db, container.trace_store, project, dataset, run_ids, page=1
                    )
                    sort_score = probe.score_names[0] if probe.score_names else ""
                if sort_score:
                    options.update(sort_score=sort_score, sort_run=params.get("sort_run"))
            result = compare.compare_runs(
                db, container.trace_store, project, dataset, run_ids, **options
            )
        except DatasetNotFound as exc:
            raise HTTPException(404, "Not found.") from exc
        except DatasetError as exc:
            error = str(exc)

    columns, rows, metrics = [], [], []
    if result:
        aggs = result.aggregates
        columns = [
            {"id": a.run_id, "name": a.name, "letter": LETTERS[i].upper(), "agg": a, "i": i}
            for i, a in enumerate(aggs)
        ]
        metrics = _metric_rows(aggs, result.score_names)
        base_id = aggs[0].run_id
        for row in result.rows:
            base_cell = row.cells.get(base_id)
            text, expected = compact(row.input), compact(row.expected_output)
            rows.append(
                {
                    "id": row.item_id,
                    "input": text,
                    "expected": expected,
                    "long": len(text) > LONG_TEXT // 2 or len(expected) > LONG_TEXT,
                    "cells": [
                        _item_cell(row.cells.get(c["id"]), row, c["id"], base_cell) for c in columns
                    ],
                    "reg": bool(row.regressed_in),
                }
            )

    def link(**over: Any) -> str:
        query: list[tuple[str, str]] = [("runs", ",".join(run_ids))]
        merged = {
            "regressed": "1" if only_regressed else "",
            "threshold": raw_threshold,
            "sort": sort if sort != "item" else "",
            "score": sort_score if sort == "delta" else "",
            **over,
        }
        query += [(k, str(v)) for k, v in merged.items() if v not in ("", None)]
        return f"{base}/compare?{urlencode(query)}"

    context = page_context(
        db,
        access,
        active="datasets",
        dataset=dataset,
        base=base,
        all_runs=all_runs,
        slots=[
            (LETTERS[i].upper(), run_ids[i] if i < len(run_ids) else "")
            for i in range(compare.MAX_RUNS)
        ],
        run_ids=run_ids,
        too_many=too_many,
        max_runs=compare.MAX_RUNS,
        error=error,
        result=result,
        columns=columns,
        metrics=metrics,
        rows=rows,
        only_regressed=only_regressed,
        threshold_text=raw_threshold or "0",
        threshold=threshold,
        sort=sort,
        sort_score=sort_score,
        reset_threshold_url=link(threshold="0"),
        prev_url=link(page=result.page - 1) if result and result.page > 1 else "",
        next_url=link(page=result.page + 1) if result and result.page < result.pages else "",
    )
    return render(request, "datasets/compare.html", context)
