"""Money and count copy: sub-cent costs keep meaningful digits, counts agree with their noun."""

from __future__ import annotations

import pytest

from traceloom.web.formatting import money, plural
from traceloom.web.pagefmt import fmt_cost


@pytest.mark.parametrize(
    ("value", "places", "shown"),
    [
        (None, 2, "–"),
        (0, 2, "$0.00"),
        (7.6, 2, "$7.60"),
        (1234.5, 2, "$1,234.50"),
        (0.0123, 2, "$0.01"),
        (0.0009, 2, "$0.0009"),
        (0.00087, 2, "$0.00087"),
        (0.0009, 4, "$0.0009"),
        (0.000042, 4, "$0.000042"),
        (0.00001234, 6, "$0.000012"),
        (1e-12, 2, "< $0.000001"),
    ],
)
def test_money_never_rounds_a_real_cost_to_zero(value, places, shown):
    assert money(value, places) == shown


@pytest.mark.parametrize(
    ("value", "shown"),
    [(None, "-"), (0, "$0.0000"), (0.1, "$0.1000"), (0.0001, "$0.0001"), (0.00004, "$0.00004")],
)
def test_page_cost_filter_keeps_sub_cent_precision(value, shown):
    assert fmt_cost(value) == shown


@pytest.mark.parametrize(
    ("n", "shown"),
    [(0, "0 traces"), (1, "1 trace"), (2, "2 traces"), (1500, "1,500 traces"), (None, "0 traces")],
)
def test_plural_agrees_with_the_count(n, shown):
    assert plural(n, "trace") == shown


def test_plural_takes_an_irregular_form():
    assert plural(1, "match", "matches") == "1 match"
    assert plural(3, "match", "matches") == "3 matches"
