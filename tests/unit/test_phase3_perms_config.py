import pytest

from traceloom.auth.permissions import MIN_ROLE, Perm, Role, can
from traceloom.config import Settings

PHASE3_MATRIX = {
    Perm.EVAL_READ: Role.VIEWER,
    Perm.SCORES_WRITE: Role.MEMBER,
    Perm.QUEUES_WRITE: Role.MEMBER,
    Perm.EVAL_TEMPLATES_WRITE: Role.MEMBER,
    Perm.DATASETS_WRITE: Role.MEMBER,
    Perm.EXPERIMENTS_RUN: Role.MEMBER,
    Perm.SCORE_CONFIGS: Role.ADMIN,
    Perm.EVAL_RULES: Role.ADMIN,
    Perm.EVAL_SETTINGS: Role.ADMIN,
    Perm.QUEUES_DELETE: Role.ADMIN,
    Perm.DATASETS_DELETE: Role.ADMIN,
}


@pytest.mark.parametrize(("perm", "minimum"), PHASE3_MATRIX.items())
def test_phase3_perm_minimum_role(perm, minimum):
    assert MIN_ROLE[perm] == minimum
    assert can(minimum, perm)
    if minimum > Role.VIEWER:
        assert not can(Role(minimum - 1), perm)
    assert can(Role.OWNER, perm)
    assert not can(None, perm)


def test_every_perm_has_a_role():
    assert set(MIN_ROLE) == set(Perm)


def test_phase3_settings_defaults():
    s = Settings(_env_file=None)
    assert s.rate_feedback == "60/minute"
    assert s.queue_bulk_max == 5000
    assert s.annotation_lock_minutes == 10
    assert s.eval_daily_cost_cap_usd == 10.0
    assert s.eval_max_attempts == 3
    assert s.eval_environment == "traceloom-eval"
    assert s.experiment_environment == "traceloom-experiment"
    assert s.eval_backfill_max == 1000
    assert s.eval_concurrency == 4
    assert s.dataset_import_max_rows == 10000
    assert s.eval_rule_cache_seconds == 10
    assert s.eval_stuck_seconds == 600
    assert s.eval_sweep_batch == 200


def test_phase3_settings_from_env(monkeypatch):
    monkeypatch.setenv("TRACELOOM_EVAL_CONCURRENCY", "7")
    monkeypatch.setenv("TRACELOOM_EVAL_DAILY_COST_CAP_USD", "2.5")
    s = Settings(_env_file=None)
    assert s.eval_concurrency == 7
    assert s.eval_daily_cost_cap_usd == 2.5
