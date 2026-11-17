from __future__ import annotations

import pytest

from traceloom.datasets import compare
from traceloom.datasets.compare import Cell, RunData, ScoreValue
from traceloom.datasets.service import DatasetError


def cell(score=None, *, latency=100.0, cost=0.01, kind="NUMERIC"):
    scores = {"s": ScoreValue(score, kind)} if score is not None else {}
    return Cell(latency_ms=latency, cost=cost, scores=scores)


def run(rid, values, **kw):
    return RunData(rid, rid, {f"i{n}": cell(v, **kw) for n, v in enumerate(values)})


def test_best_marker_per_score():
    a = RunData("a", "a", {"i0": Cell(scores={"x": ScoreValue(0.5), "y": ScoreValue(1.0)})})
    b = RunData("b", "b", {"i0": Cell(scores={"x": ScoreValue(0.9), "y": ScoreValue(0.2)})})
    sa, sb = compare.build([a, b], {}).aggregates
    assert (sa.scores["x"].best, sb.scores["x"].best) == (False, True)
    assert (sa.scores["y"].best, sb.scores["y"].best) == (True, False)


def test_run_aggregate_stats():
    cells = {
        f"i{n}": Cell(
            latency_ms=float(n),
            cost=0.5,
            status="completed" if n < 3 else "failed",
            scores={"s": ScoreValue(n / 10)},
        )
        for n in range(4)
    }
    agg = compare.aggregate(RunData("r", "r", cells))
    assert agg.scores["s"].n == 4 and agg.scores["s"].mean == pytest.approx(0.15)
    assert agg.success_rate == 0.75
    assert agg.mean_latency_ms == 1.5 and agg.p95_latency_ms == pytest.approx(2.85)
    assert agg.total_cost == pytest.approx(2.0)


def test_regressed_exactly_the_lower_items():
    base = run("a", [0.5, 0.5, 0.5, 0.5])
    cand = run("b", [0.9, 0.4, 0.5, 0.6])
    out = compare.build([base, cand], {}, only_regressed=True)
    assert [r.item_id for r in out.rows] == ["i1"]
    assert out.aggregates[1].regressed == 1 and out.aggregates[0].regressed == 0


def test_threshold_hides_small_deltas():
    base = run("a", [0.5, 0.5])
    cand = run("b", [0.45, 0.2])
    assert compare.build([base, cand], {}, only_regressed=True).total_items == 2
    out = compare.build([base, cand], {}, threshold=0.1, only_regressed=True)
    assert [r.item_id for r in out.rows] == ["i1"]
    assert compare.build([base, cand], {}, threshold=0.3, only_regressed=True).total_items == 0


def test_boolean_true_to_false_regresses_even_with_high_threshold():
    base = run("a", [1.0, 0.0, 1.0], kind="BOOLEAN")
    cand = run("b", [0.0, 1.0, 1.0], kind="BOOLEAN")
    out = compare.build([base, cand], {}, threshold=5, only_regressed=True)
    assert [r.item_id for r in out.rows] == ["i0"]


def test_pagination_is_100_per_page():
    base = run("a", [0.5] * 250)
    cand = run("b", [0.4] * 250)
    sizes = [len(compare.build([base, cand], {}, page=p).rows) for p in (1, 2, 3)]
    assert sizes == [100, 100, 50]
    out = compare.build([base, cand], {}, page=9)
    assert out.page == 3 and out.pages == 3 and out.total_items == 250
    assert out.aggregates[1].regressed == 250  # counted over all pages


def test_sort_by_delta_worst_first_missing_last():
    base = run("a", [0.5, 0.5, 0.5])
    cand = run("b", [0.6, 0.1, None])
    out = compare.build([base, cand], {}, sort_score="s")
    assert [r.item_id for r in out.rows] == ["i1", "i0", "i2"]
    out = compare.build([base, cand], {}, sort_score="s", descending=True)
    assert [r.item_id for r in out.rows] == ["i0", "i1", "i2"]


def test_needs_a_run():
    with pytest.raises(DatasetError):
        compare.build([], {})
