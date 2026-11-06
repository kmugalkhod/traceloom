from __future__ import annotations

from datetime import UTC, datetime

import pytest

from traceloom.cost.pricing import compute_cost

START = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

# FR-X3: model -> (input, output, cache_read or None), USD per million tokens.
FR_X3 = {
    "claude-fable-5-1": (10, 50, 0.25),
    "claude-fable-5": (10, 50, None),
    "claude-opus-5-5": (4, 20, 0.20),
    "claude-opus-5": (5, 25, None),
    "claude-opus-4-8": (5, 25, None),
    "claude-opus-4-7": (5, 25, None),
    "claude-opus-4-6": (5, 25, None),
    "claude-sonnet-5-5": (2, 10, 0.20),
    "claude-sonnet-5": (2, 10, None),
    "claude-sonnet-4-6": (3, 15, None),
    "claude-haiku-4-5": (1, 5, None),
}


@pytest.fixture
def registry(container, client):
    return container.models


@pytest.mark.parametrize(("name", "prices"), FR_X3.items())
def test_fr_x3_models_resolve_with_the_specified_prices(registry, name, prices):
    definition = registry.find(None, name, START)
    assert definition is not None and definition.source == "seed"
    assert definition.model_name == name
    per_million = prices[:2]
    assert definition.prices["input"] == pytest.approx(per_million[0] / 1e6)
    assert definition.prices["output"] == pytest.approx(per_million[1] / 1e6)
    if prices[2] is not None:
        assert definition.prices["cache_read"] == pytest.approx(prices[2] / 1e6)


@pytest.mark.parametrize("name", [n for n in FR_X3 if n != "claude-haiku-4-5"])
def test_new_entries_are_valid_from_2026_and_cache_write_is_1_25x_input(registry, name):
    definition = registry.find(None, name, START)
    assert definition.valid_from == datetime(2026, 1, 1, tzinfo=UTC)
    assert definition.prices["cache_write"] == pytest.approx(definition.prices["input"] * 1.25)
    assert registry.find(None, name, datetime(2025, 6, 1, tzinfo=UTC)) is None


def test_sonnet_5_5_costs_2_and_10_dollars_per_million(registry):
    definition = registry.find(None, "claude-sonnet-5-5", START)
    cost = compute_cost(definition, {"input": 1_000_000, "output": 1_000_000})
    assert cost["input"] == pytest.approx(2.0)
    assert cost["output"] == pytest.approx(10.0)
    assert cost["total"] == pytest.approx(12.0)


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("claude-sonnet-5-5-20260301", "claude-sonnet-5-5"),
        ("anthropic/claude-opus-5-5", "claude-opus-5-5"),
        ("CLAUDE-FABLE-5-1", "claude-fable-5-1"),
        ("claude-opus-4-8-latest", "claude-opus-4-8"),
        ("claude-sonnet-5", "claude-sonnet-5"),
    ],
)
def test_dated_and_prefixed_ids_resolve_to_the_right_entry(registry, alias, expected):
    assert registry.find(None, alias, START).model_name == expected


def test_neighbouring_families_do_not_collide(registry):
    assert registry.find(None, "claude-sonnet-5-5", START).model_name == "claude-sonnet-5-5"
    assert registry.find(None, "claude-opus-4-5", START).model_name == "claude-opus-4-5"
    assert registry.find(None, "claude-opus-4", START).model_name == "claude-opus-4"
    assert registry.find(None, "claude-fable-6", START) is None
