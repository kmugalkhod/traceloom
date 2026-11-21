"""Run comparison (FR-D8): per-run aggregates, the per-item matrix, regressions against the
baseline (the first run) and sorting by score delta.

The math works on plain `RunData` / `Cell` values so it is testable without a database;
`compare_runs` loads them through the run service and the `TraceStore` port.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from traceloom.datasets import runs as runs_service
from traceloom.datasets.service import DatasetError
from traceloom.db.models import Project
from traceloom.db.models_datasets import Dataset
from traceloom.ports import ScoreQuery, ScoreRow, TraceStore
from traceloom.scores.analytics import percentile

PAGE_SIZE = 100
MAX_RUNS = 4
BOOLEAN = "BOOLEAN"


@dataclass(frozen=True)
class ScoreValue:
    """One score of an item in one run: the mean of its numeric values and its data type."""

    value: float
    data_type: str = "NUMERIC"


@dataclass
class Cell:
    """What one run did with one item."""

    status: str = "completed"
    output: Any = None
    latency_ms: float | None = None
    cost: float | None = None
    error: str | None = None
    scores: dict[str, ScoreValue] = field(default_factory=dict)


@dataclass
class RunData:
    run_id: str
    name: str
    cells: dict[str, Cell]  # item id -> cell


@dataclass
class ScoreStat:
    mean: float
    n: int
    best: bool = False


@dataclass
class RunAggregate:
    run_id: str
    name: str
    scores: dict[str, ScoreStat]
    items: int
    success_rate: float | None
    mean_latency_ms: float | None
    p95_latency_ms: float | None
    total_cost: float
    regressed: int = 0


@dataclass
class ItemRow:
    item_id: str
    input: Any
    expected_output: Any
    cells: dict[str, Cell]  # run id -> cell
    regressed_in: set[str]  # run ids where this item regresses
    deltas: dict[str, dict[str, float]]  # run id -> score name -> delta vs baseline


@dataclass
class Comparison:
    aggregates: list[RunAggregate]
    rows: list[ItemRow]  # the requested page
    score_names: list[str]
    total_items: int  # after the "only regressed" filter
    page: int
    pages: int
    threshold: float


# --- math ---------------------------------------------------------------------------------


def aggregate(data: RunData) -> RunAggregate:
    cells = list(data.cells.values())
    values: dict[str, list[float]] = {}
    for cell in cells:
        for name, score in cell.scores.items():
            values.setdefault(name, []).append(score.value)
    latencies = sorted(c.latency_ms for c in cells if c.latency_ms is not None)
    return RunAggregate(
        run_id=data.run_id,
        name=data.name,
        scores={n: ScoreStat(statistics.fmean(v), len(v)) for n, v in values.items()},
        items=len(cells),
        success_rate=(
            sum(1 for c in cells if c.status == "completed") / len(cells) if cells else None
        ),
        mean_latency_ms=statistics.fmean(latencies) if latencies else None,
        p95_latency_ms=percentile(latencies, 95) if latencies else None,
        total_cost=sum(c.cost or 0.0 for c in cells),
    )


def mark_best(aggregates: list[RunAggregate]) -> None:
    """Mark, per score name, the run(s) with the highest mean (ties are all best)."""
    names = {n for a in aggregates for n in a.scores}
    for name in names:
        top = max(a.scores[name].mean for a in aggregates if name in a.scores)
        for a in aggregates:
            if name in a.scores and a.scores[name].mean == top:
                a.scores[name].best = True


def score_delta(base: Cell | None, cell: Cell | None, name: str) -> float | None:
    if base is None or cell is None or name not in base.scores or name not in cell.scores:
        return None
    return cell.scores[name].value - base.scores[name].value


def regressed(base: Cell | None, cell: Cell | None, threshold: float = 0.0) -> bool:
    """Any score lower than the baseline's by more than the threshold; a boolean true→false
    always regresses."""
    if base is None or cell is None:
        return False
    for name, score in cell.scores.items():
        before = base.scores.get(name)
        if before is None:
            continue
        drop = before.value - score.value
        if before.data_type == BOOLEAN and score.data_type == BOOLEAN:
            if before.value > 0 and score.value <= 0:
                return True
        elif drop > threshold:
            return True
    return False


def build(
    runs: list[RunData],
    item_info: dict[str, tuple[Any, Any]],
    *,
    threshold: float = 0.0,
    only_regressed: bool = False,
    sort_score: str | None = None,
    sort_run: str | None = None,
    descending: bool = False,
    page: int = 1,
) -> Comparison:
    if not runs:
        raise DatasetError("Pick at least one run to compare.")
    base = runs[0]
    aggregates = [aggregate(r) for r in runs]
    mark_best(aggregates)
    others = runs[1:]

    item_ids = list(base.cells)
    seen = set(item_ids)
    for r in others:
        item_ids += [i for i in r.cells if i not in seen]
        seen.update(r.cells)

    rows: list[ItemRow] = []
    for item_id in item_ids:
        cells = {r.run_id: r.cells[item_id] for r in runs if item_id in r.cells}
        base_cell = base.cells.get(item_id)
        bad = {r.run_id for r in others if regressed(base_cell, r.cells.get(item_id), threshold)}
        deltas = {}
        for r in others:
            per = {
                n: d
                for n in (r.cells.get(item_id).scores if item_id in r.cells else {})
                if (d := score_delta(base_cell, r.cells.get(item_id), n)) is not None
            }
            deltas[r.run_id] = per
        inp, expected = item_info.get(item_id, (None, None))
        rows.append(ItemRow(item_id, inp, expected, cells, bad, deltas))

    by_id = {a.run_id: a for a in aggregates}
    for row in rows:
        for run_id in row.regressed_in:
            by_id[run_id].regressed += 1

    if only_regressed:
        rows = [r for r in rows if r.regressed_in]
    if sort_score and others:
        target = sort_run if sort_run in {r.run_id for r in others} else others[0].run_id

        def key(row: ItemRow) -> tuple[bool, float]:
            d = row.deltas.get(target, {}).get(sort_score)
            return (d is None, 0.0 if d is None else (-d if descending else d))

        rows.sort(key=key)  # missing deltas last; lowest (worst) delta first by default

    total = len(rows)
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(page, 1), pages)
    names = sorted({n for a in aggregates for n in a.scores})
    start = (page - 1) * PAGE_SIZE
    return Comparison(
        aggregates, rows[start : start + PAGE_SIZE], names, total, page, pages, threshold
    )


# --- loading ------------------------------------------------------------------------------


def _fold(scores: list[ScoreRow]) -> dict[str, ScoreValue]:
    grouped: dict[str, list[ScoreRow]] = {}
    for s in scores:
        if s.value is not None and s.data_type != "CATEGORICAL":
            grouped.setdefault(s.name, []).append(s)
    return {
        name: ScoreValue(statistics.fmean(s.value for s in rows), rows[0].data_type)
        for name, rows in grouped.items()
    }


def _run_scores(store: TraceStore, project_id: str, run_id: str) -> dict[str, list[ScoreRow]]:
    by_trace: dict[str, list[ScoreRow]] = {}
    cursor = None
    while True:
        page = store.list_scores(
            project_id, ScoreQuery(dataset_run_id=run_id, cursor=cursor, limit=200)
        )
        for s in page.items:
            if s.trace_id:
                by_trace.setdefault(s.trace_id, []).append(s)
        cursor = page.next_cursor
        if not cursor:
            return by_trace


def load_run(db: Session, store: TraceStore, project: Project, run_id: str) -> RunData:
    run = runs_service.get_run(db, project, run_id)
    by_trace = _run_scores(store, project.id, run.id)
    cells: dict[str, Cell] = {}
    for ri in runs_service.run_items(db, project, run):  # a re-linked item keeps its last link
        cells[ri.item_id] = Cell(
            status=ri.status,
            output=ri.output,
            latency_ms=ri.latency_ms,
            cost=ri.cost if ri.cost is not None else ri.estimated_cost,
            error=ri.error,
            scores=_fold(by_trace.get(ri.trace_id, [])) if ri.trace_id else {},
        )
    return RunData(run.id, run.name, cells)


def compare_runs(
    db: Session,
    store: TraceStore,
    project: Project,
    dataset: Dataset,
    run_ids: list[str],
    **options: Any,
) -> Comparison:
    run_ids = list(dict.fromkeys(run_ids))
    if not 1 <= len(run_ids) <= MAX_RUNS:
        raise DatasetError(f"Compare between 1 and {MAX_RUNS} runs.")
    data = [load_run(db, store, project, rid) for rid in run_ids]
    info: dict[str, tuple[Any, Any]] = {}
    for rid in run_ids:
        run = runs_service.get_run(db, project, rid)
        if run.dataset_id != dataset.id:
            raise DatasetError("A run belongs to a different dataset.")
        for view in runs_service.run_item_source(db, project, run):
            info.setdefault(view.id, (view.input, view.expected_output))
    return build(data, info, **options)
