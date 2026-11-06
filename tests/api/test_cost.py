from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
import yaml

from traceloom.cost import admin
from traceloom.cost.pricing import apply_cost, compute_cost
from traceloom.cost.registry import SEED_PATH, ModelRegistry
from traceloom.cost.tokenizers import count_tokens
from traceloom.db.models import ModelDefinition
from traceloom.ports import ObservationRow

START = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def registry(container, client) -> ModelRegistry:
    return container.models  # seeded by startup


@pytest.fixture
def p1(make_org, make_project) -> str:
    return make_project(make_org()).id


@pytest.fixture
def p2(make_org, make_project) -> str:
    return make_project(make_org()).id


def gen(model="claude-sonnet-4-5", **kw) -> ObservationRow:
    return ObservationRow(
        id="o", trace_id="t", start_time=START, type="generation", model=model, **kw
    )


def fake_counter(tokenizer, text):
    return len(text.split())


# --- seed ---


def test_seed_covers_the_model_families(registry):
    names = {m["model_name"] for m in yaml.safe_load(SEED_PATH.read_text())["models"]}
    for family in ("claude-", "gpt-", "gemini-", "llama-"):
        assert any(n.startswith(family) for n in names), family
    for m in registry.find(None, "gpt-4o", START), registry.find(None, "gemini-2.5-pro", START):
        assert m is not None and m.source == "seed" and m.project_id is None


def test_seed_is_idempotent_and_every_pattern_matches_exactly_one_definition(registry, container):
    first = registry.load_seed()
    assert registry.load_seed() == first
    with container.session_factory() as s:
        assert s.query(ModelDefinition).count() == first
    entries = yaml.safe_load(SEED_PATH.read_text())["models"]
    patterns = [(e["model_name"], re.compile(e["match"])) for e in entries]
    for name, _ in patterns:
        assert [n for n, rx in patterns if rx.search(name)] == [name], name
    for alias in (
        "anthropic/claude-sonnet-4-5-20250929",
        "claude-opus-4-1-20250805",
        "gpt-4o-2024-08-06",
        "models/gemini-2.5-flash",
        "anthropic.claude-3-5-haiku-20241022-v1:0",
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    ):
        assert registry.find(None, alias, START) is not None, alias


def test_seed_prices_are_sane(registry):
    for e in yaml.safe_load(SEED_PATH.read_text())["models"]:
        assert 0 < e["prices"]["input"] <= e["prices"]["output"] < 1e-3
        for tier in e.get("tiers") or []:
            assert tier["threshold"] > 0 and tier["prices"]["input"] >= e["prices"]["input"]


# --- cost ---


def test_provided_cost_wins(registry):
    obs = gen(
        usage_details={"input": 1000, "output": 1000},
        usage_source="provided",
        cost_details={"input": 0.5, "output": 0.25},
        cost_source="provided",
    )
    apply_cost(obs, "p1", registry)
    assert obs.cost_source == "provided"
    assert obs.cost_details == {"input": 0.5, "output": 0.25, "total": 0.75}


def test_calculated_cost_with_total(registry):
    obs = gen(usage_details={"input": 1000, "output": 500, "total": 1500}, usage_source="provided")
    apply_cost(obs, "p1", registry)
    assert obs.cost_source == "calculated"
    assert obs.cost_details["input"] == pytest.approx(1000 * 3e-6)
    assert obs.cost_details["output"] == pytest.approx(500 * 15e-6)
    assert obs.cost_details["total"] == pytest.approx(0.003 + 0.0075)
    assert obs.usage_source == "provided"


def test_tier_applies_above_the_threshold(registry):
    d = registry.find(None, "gemini-2.5-pro", START)
    below = compute_cost(d, {"input": 200_000, "output": 1000})
    above = compute_cost(d, {"input": 200_001, "output": 1000})
    assert below["input"] == pytest.approx(200_000 * 1.25e-6)
    assert above["input"] == pytest.approx(200_001 * 2.5e-6)
    assert above["output"] == pytest.approx(1000 * 15e-6)


def test_no_matching_model_means_no_cost(registry):
    for model in ("totally-unknown-model", None):
        obs = gen(model, usage_details={"input": 10}, usage_source="provided")
        apply_cost(obs, "p1", registry)
        assert (obs.cost_source, obs.cost_details) == ("none", {})


def test_usage_without_priced_keys_means_no_cost(registry):
    obs = gen(usage_details={"images": 3}, usage_source="provided")
    apply_cost(obs, "p1", registry)
    assert obs.cost_source == "none"


def test_apply_is_idempotent(registry):
    obs = gen(usage_details={"input": 10, "output": 5}, usage_source="provided")
    apply_cost(obs, "p1", registry)
    once = (dict(obs.cost_details), obs.cost_source)
    apply_cost(obs, "p1", registry)
    assert (obs.cost_details, obs.cost_source) == once


# --- precedence ---


def test_project_override_beats_global(p1, p2, registry, container, db):
    admin.create_definition(
        db,
        registry,
        p1,
        model_name="sonnet override",
        match_pattern=r"^claude-sonnet-4-5$",
        prices={"input": 1e-6, "output": 2e-6},
    )
    db.commit()
    obs = gen(usage_details={"input": 1000, "output": 1000}, usage_source="provided")
    apply_cost(obs, p1, registry)
    assert obs.cost_details["total"] == pytest.approx(0.003)
    other = gen(usage_details={"input": 1000, "output": 1000}, usage_source="provided")
    apply_cost(other, p2, registry)  # another project still sees the global price
    assert other.cost_details["total"] == pytest.approx(0.018)


def test_latest_valid_from_not_after_start_wins(p1, registry, db):
    def add(pattern, price, year):
        admin.create_definition(
            db,
            registry,
            p1,
            model_name=f"m{year}",
            match_pattern=pattern,
            prices={"input": price},
            valid_from=datetime(year, 1, 1, tzinfo=UTC),
        )

    add("^my-model$", 1.0, 2025)
    add("^my-model$", 2.0, 2026)
    add("^my-model$", 3.0, 2027)
    db.commit()
    assert registry.find(p1, "my-model", datetime(2026, 6, 1, tzinfo=UTC)).prices["input"] == 2.0
    assert registry.find(p1, "my-model", datetime(2027, 6, 1, tzinfo=UTC)).prices["input"] == 3.0
    assert registry.find(p1, "my-model", datetime(2024, 6, 1, tzinfo=UTC)) is None


# --- tokenizer ---


def test_tokenizer_fills_usage_when_missing(p1, registry):
    obs = gen("gpt-4o", input=[{"role": "user", "content": "hello there friend"}], output="hi you")
    apply_cost(obs, p1, registry, counter=fake_counter)
    assert obs.usage_source == "tokenizer"
    assert obs.usage_details == {"input": 3, "output": 2, "total": 5}
    assert obs.cost_source == "calculated" and obs.cost_details["total"] > 0


def test_unavailable_tokenizer_leaves_usage_empty(p1, registry):
    obs = gen("gpt-4o", input="hello", output="world")
    apply_cost(obs, p1, registry, counter=lambda t, s: None)
    assert (obs.usage_details, obs.usage_source, obs.cost_source) == ({}, "none", "none")


def test_real_tokenizer_degrades_offline(monkeypatch):
    import traceloom.cost.tokenizers as tk

    monkeypatch.setattr(tk, "_encoders", {})

    def boom(name):
        raise OSError("no network")

    import tiktoken

    monkeypatch.setattr(tiktoken, "get_encoding", boom)
    assert count_tokens("tiktoken:o200k_base", "hello") is None
    assert count_tokens(None, "hello") is None


def test_models_without_tokenizer_get_no_counted_usage(p1, registry):
    obs = gen("claude-sonnet-4-5", input="hello", output="world")
    apply_cost(obs, p1, registry, counter=fake_counter)
    assert obs.usage_source == "none" and obs.cost_source == "none"


# --- admin ---


def test_admin_crud_and_validation(p1, registry, db):
    row = admin.create_definition(
        db,
        registry,
        p1,
        model_name="mine",
        match_pattern="^mine$",
        prices={"input": 1e-6},
        tiers=[{"threshold": 10, "prices": {"input": 2e-6}}],
    )
    db.commit()
    assert [r.model_name for r in admin.list_definitions(db, p1)][0] == "mine"
    assert any(r.project_id is None for r in admin.list_definitions(db, p1))
    admin.update_definition(db, registry, p1, row.id, prices={"input": 5e-6})
    db.commit()
    assert registry.find(p1, "mine", START).prices == {"input": 5e-6}
    for bad in (
        dict(model_name="", match_pattern="x", prices={"input": 1}),
        dict(model_name="a", match_pattern="(", prices={"input": 1}),
        dict(model_name="a", match_pattern="x", prices={"input": -1}),
        dict(model_name="a", match_pattern="x", prices={}),
        dict(
            model_name="a",
            match_pattern="x",
            prices={"input": 1},
            tiers=[{"threshold": 0, "prices": {"input": 1}}],
        ),
        dict(
            model_name="a", match_pattern="x", prices={"input": 1}, valid_from=datetime(2026, 1, 1)
        ),
    ):
        with pytest.raises(admin.DefinitionError):
            admin.create_definition(db, registry, p1, **bad)
    admin.delete_definition(db, registry, p1, row.id)
    db.commit()
    assert registry.find(p1, "mine", START) is None


def test_globals_and_other_projects_are_not_editable(p1, p2, registry, db):
    glob = registry.find(None, "gpt-4o", START)
    mine = admin.create_definition(
        db, registry, p1, model_name="m", match_pattern="^m$", prices={"input": 1}
    )
    for pid, did in ((p1, glob.id), (p2, mine.id)):
        with pytest.raises(admin.DefinitionNotFound):
            admin.update_definition(db, registry, pid, did, prices={"input": 9})
        with pytest.raises(admin.DefinitionNotFound):
            admin.delete_definition(db, registry, pid, did)
