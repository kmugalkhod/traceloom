import re
from datetime import UTC, datetime

import pytest

from traceloom.config import Settings
from traceloom.cost.registry import ModelDef, Tier
from traceloom.evals import spend
from traceloom.evals.spend import CapReached, Estimate

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def model(prices=None, tiers=(), tokenizer=None):
    return ModelDef(
        "m1",
        None,
        "judge",
        re.compile("judge"),
        tokenizer,
        {"input": 2e-6, "output": 1e-5} if prices is None else prices,
        tuple(tiers),
    )


def test_estimate_is_prompt_times_input_plus_max_tokens_times_output():
    est = spend.estimate_cost(model(), prompt_tokens=1000, max_tokens=200)
    assert est.priced and est.cost == pytest.approx(1000 * 2e-6 + 200 * 1e-5)


def test_estimate_uses_the_tier_price_and_a_default_max_tokens():
    tiered = model(tiers=[Tier(500, {"input": 4e-6})])
    est = spend.estimate_cost(tiered, prompt_tokens=1000)
    assert est.cost == pytest.approx(1000 * 4e-6 + spend.DEFAULT_MAX_TOKENS * 1e-5)


def test_an_unpriced_model_estimates_to_zero_and_is_flagged():
    assert spend.estimate_cost(None, 1000, 100) == Estimate(0.0, False)
    assert spend.estimate_cost(model(prices={}), 1000, 100) == Estimate(0.0, False)


def test_prompt_tokens_fall_back_to_a_character_estimate():
    assert spend.count_prompt_tokens(None, "x" * 40) == 10
    assert spend.count_prompt_tokens(model(tokenizer="nope:x"), "x" * 41) == 11


def test_cap_defaults_to_the_env_value_and_a_project_override_wins(db, world, settings):
    pid = world.project.id
    assert spend.get_cap(db, pid, settings) == settings.eval_daily_cost_cap_usd == 10.0
    spend.set_cap(db, pid, 2.5)
    assert spend.get_cap(db, pid, settings) == 2.5
    spend.set_cap(db, pid, 0)  # 0 is a real override, not "unset"
    assert spend.get_cap(db, pid, settings) == 0
    spend.set_cap(db, pid, None)
    assert spend.get_cap(db, pid, settings) == 10.0
    with pytest.raises(ValueError):
        spend.set_cap(db, pid, -1)


def test_cap_env_default_is_configurable(db, world, tmp_path, encryption_key):
    custom = Settings(
        TRACELOOM_ENCRYPTION_KEY=encryption_key,
        TRACELOOM_EVAL_DAILY_COST_CAP_USD="3",
        DATABASE_URL=f"sqlite:///{tmp_path / 'x.db'}",
    )
    assert spend.get_cap(db, world.project.id, custom) == 3.0


def test_reserve_settle_release_move_the_ledger(db, container, world, settings):
    sessions, pid = container.session_factory, world.project.id
    first = spend.reserve(sessions, pid, Estimate(0.4, True), cap=1.0, now=NOW)
    second = spend.reserve(sessions, pid, Estimate(0.5, True), cap=1.0, now=NOW)
    state = spend.status(db, pid, settings, NOW)
    assert (state.spent, state.reserved) == (0, pytest.approx(0.9))

    with pytest.raises(CapReached, match=r"daily cost cap of \$1 reached"):
        spend.reserve(sessions, pid, Estimate(0.2, True), cap=1.0, now=NOW)

    spend.settle(sessions, first, actual=0.1)  # the estimate is replaced by the actual cost
    db.expire_all()
    state = spend.status(db, pid, settings, NOW)
    assert (state.spent, state.reserved) == (pytest.approx(0.1), pytest.approx(0.5))

    spend.release(sessions, second)  # a call that never ran frees its hold
    db.expire_all()
    state = spend.status(db, pid, settings, NOW)
    assert (state.spent, state.reserved) == (pytest.approx(0.1), pytest.approx(0))
    spend.reserve(sessions, pid, Estimate(0.9, True), cap=1.0, now=NOW)  # exactly fills the cap


def test_a_cap_of_zero_rejects_every_reservation(container, world):
    sessions, pid = container.session_factory, world.project.id
    for estimate in (Estimate(0.01, True), Estimate(0.0, False)):
        with pytest.raises(CapReached):
            spend.reserve(sessions, pid, estimate, cap=0, now=NOW)


def test_unpriced_calls_are_allowed_without_touching_the_ledger(db, container, world, settings):
    sessions, pid = container.session_factory, world.project.id
    spend.reserve(sessions, pid, Estimate(0.5, True), cap=0.5, now=NOW)  # cap is now full
    held = spend.reserve(sessions, pid, Estimate(0.0, False), cap=0.5, now=NOW)
    assert held.priced is False
    spend.settle(sessions, held, 0.0)
    spend.release(sessions, held)
    assert spend.status(db, pid, settings, NOW).reserved == pytest.approx(0.5)


def test_day_rolls_over_at_utc_midnight(db, container, world, settings):
    sessions, pid = container.session_factory, world.project.id
    late = datetime(2026, 11, 9, 23, 59, 59, tzinfo=UTC)
    early = datetime(2026, 11, 10, 0, 0, 1, tzinfo=UTC)
    held = spend.reserve(sessions, pid, Estimate(1.0, True), cap=1.0, now=late)
    with pytest.raises(CapReached):
        spend.reserve(sessions, pid, Estimate(0.1, True), cap=1.0, now=late)
    spend.reserve(sessions, pid, Estimate(1.0, True), cap=1.0, now=early)  # a fresh day

    spend.settle(sessions, held, 0.7)  # settles against the day it was reserved on
    db.expire_all()
    before, after = spend.status(db, pid, settings, late), spend.status(db, pid, settings, early)
    assert (before.day.isoformat(), before.spent, before.reserved) == (
        "2026-11-09",
        pytest.approx(0.7),
        0,
    )
    assert (after.day.isoformat(), after.spent, after.reserved) == (
        "2026-11-10",
        0,
        pytest.approx(1.0),
    )


def test_status_reports_paused_and_reached(db, world, settings):
    pid = world.project.id
    spend.set_cap(db, pid, 0)
    assert spend.status(db, pid, settings, NOW).paused
    spend.set_cap(db, pid, 1)
    assert not spend.status(db, pid, settings, NOW).reached
