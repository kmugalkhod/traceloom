import random
import statistics
from datetime import datetime

import pytest

from traceloom.scores import analytics as an

RNG = random.Random(7)
XS = [RNG.uniform(0, 10) for _ in range(40)]
YS = [x * 0.6 + RNG.uniform(0, 5) for x in XS]


def test_pearson_matches_statistics():
    assert an.pearson(XS, YS)["r"] == pytest.approx(statistics.correlation(XS, YS), abs=1e-9)


def test_spearman_matches_statistics_with_ties():
    xs = [1, 2, 2, 3, 4, 4, 4, 5, 6, 7, 8, 8]
    ys = [2, 1, 3, 3, 5, 4, 6, 6, 7, 9, 8, 10]
    expected = statistics.correlation(xs, ys, method="ranked")
    assert an.spearman(xs, ys)["rho"] == pytest.approx(expected, abs=1e-9)
    assert an.spearman(XS, YS)["rho"] == pytest.approx(
        statistics.correlation(XS, YS, method="ranked"), abs=1e-9
    )


def test_constant_side_has_no_correlation():
    assert an.pearson([1.0] * 10, list(range(10)))["r"] is None


def test_ranks_average_ties():
    assert an.ranks([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0]


def test_percentiles():
    r = an.summary(list(range(1, 12)))  # 1..11
    assert (r["mean"], r["median"], r["p10"], r["p90"]) == (6.0, 6.0, 2.0, 10.0)
    vals = [3.1, 9.4, 2.2, 7.7, 5.0, 1.0, 8.8, 4.4, 6.6, 0.5, 9.9]
    r = an.summary(vals)
    q = statistics.quantiles(vals, n=10, method="inclusive")
    assert r["p10"] == pytest.approx(q[0]) and r["p90"] == pytest.approx(q[-1])
    assert r["median"] == statistics.median(vals)
    assert (r["min"], r["max"]) == (0.5, 9.9)


def test_percentile_interpolates():
    assert an.percentile([0, 10], 25) == 2.5


def test_histogram_bins():
    vals = list(range(0, 11)) + [10]  # 12 values, 0..10
    h = an.histogram(vals)
    counts = [b["count"] for b in h["bins"]]
    assert len(counts) == 10 and sum(counts) == 12
    assert counts[-1] == 3  # 9.0 <= v <= 10 -> 9, 10, 10
    assert h["bins"][0]["start"] == 0 and h["bins"][-1]["end"] == 10


def test_histogram_constant_values():
    h = an.histogram([5.0] * 10)
    assert h["bins"][0]["count"] == 10


def test_trend_buckets_and_gaps():
    pts = [(datetime(2026, 1, 1, 10, m), float(m)) for m in range(0, 50, 5)]  # 10 points
    pts.append((datetime(2026, 1, 1, 12, 30), 100.0))
    t = an.trend(pts, "hour")
    assert [p["count"] for p in t["points"]] == [10, 0, 1]
    assert t["points"][0]["mean"] == 22.5 and t["points"][1]["mean"] is None
    d = an.trend(pts + [(datetime(2026, 1, 3, 1), 4.0)], "day")
    assert [p["count"] for p in d["points"]] == [11, 0, 1]
    with pytest.raises(ValueError):
        an.trend(pts, "week")  # type: ignore[arg-type]


A = list("yyyyyyyyyynnnnnnnnnn")
B = list("yyyyyyyynnnyyynnnnnn")
# a=y: 8 y + 2 n ; a=n: 3 y + 7 n  -> agree 15/20


def test_confusion_matrix_hand_computed():
    r = an.confusion_matrix(A, B)
    assert r["labels"] == ["n", "y"]
    assert r["matrix"] == [[7, 3], [2, 8]]


def test_agreement_and_kappa_hand_computed():
    r = an.agreement(A, B)
    assert r["agreement_pct"] == 75.0
    # pe = (10*10 + 10*10)/400 ... a: y10 n10; b: y11 n9 -> (110 + 90)/400 = 0.5
    assert r["kappa"] == pytest.approx((0.75 - 0.5) / 0.5)


def test_kappa_perfect_and_degenerate():
    labels = ["a", "b"] * 5
    assert an.agreement(labels, labels)["kappa"] == 1.0
    assert an.agreement(["a"] * 10, ["a"] * 10)["kappa"] is None


def test_box_by_category():
    cats = ["x"] * 5 + ["y"] * 5
    vals = [1, 2, 3, 4, 5, 10, 20, 30, 40, 50]
    r = an.box_by_category(cats, vals)["boxes"]
    assert r["x"] == {"n": 5, "min": 1.0, "q1": 2.0, "median": 3.0, "q3": 4.0, "max": 5.0}
    assert r["y"]["median"] == 30.0


@pytest.mark.parametrize(
    "call",
    [
        lambda: an.summary(list(range(9))),
        lambda: an.histogram(list(range(9))),
        lambda: an.trend([(datetime(2026, 1, 1), 1.0)] * 9),
        lambda: an.pearson(list(range(9)), list(range(9))),
        lambda: an.spearman(list(range(9)), list(range(9))),
        lambda: an.confusion_matrix(["a"] * 9, ["a"] * 9),
        lambda: an.agreement(["a"] * 9, ["a"] * 9),
        lambda: an.box_by_category(["a"] * 9, [1.0] * 9),
    ],
)
def test_fewer_than_ten_is_not_enough_data(call):
    r = call()
    assert r["status"] == "not-enough-data" and r["n"] == 9


def test_ten_is_enough():
    assert an.summary(list(range(10)))["status"] == "ok"


def test_length_mismatch_raises():
    with pytest.raises(ValueError):
        an.pearson([1] * 10, [1] * 11)
