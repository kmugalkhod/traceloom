"""The score analytics page: distributions, trend, compare panel, empty and not-enough states."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from traceloom.auth.permissions import Role
from traceloom.db.models import Score

NOW = datetime.now(UTC)


def pid(world):
    return world.project.id


def add(db, world, sid, name, trace, source, *, value=None, text=None, kind="NUMERIC",
        ago=timedelta(hours=2), env="production", channel=None):  # fmt: skip
    db.add(
        Score(
            project_id=pid(world), id=sid, trace_id=trace, name=name, value=value,
            string_value=text, data_type=kind, source=source, timestamp=NOW - ago,
            environment=env, channel=channel,
        )
    )  # fmt: skip


def paired_bools(db, world, matrix, name="correct", prefix="t"):
    """matrix = (tt, tf, ft, ff): human rows, judge columns."""
    i = 0
    for (human, judge), count in zip(((1, 1), (1, 0), (0, 1), (0, 0)), matrix, strict=True):
        for _ in range(count):
            i += 1
            add(
                db,
                world,
                f"{prefix}h{i}",
                name,
                f"{prefix}{i}",
                "ANNOTATION",
                value=human,
                kind="BOOLEAN",
            )
            add(
                db,
                world,
                f"{prefix}j{i}",
                name,
                f"{prefix}{i}",
                "EVAL",
                value=judge,
                kind="BOOLEAN",
            )
    db.commit()


def page(client, login, world, query="", role=Role.VIEWER):
    login(client, world.user(role), csrf=False)
    return client.get(f"/project/{pid(world)}/scores/analytics{query}")


def test_viewer_can_read_and_empty_state(client, login, world):
    r = page(client, login, world)
    assert r.status_code == 200
    assert "No scores in this range" in r.text
    assert "Set up a judge" in r.text


def test_human_vs_judge_preset_shows_matrix_agreement_and_kappa(client, login, world, db):
    paired_bools(db, world, (17, 3, 4, 6))
    r = page(client, login, world, "?preset=human-judge&score=correct")
    assert r.status_code == 200
    assert "Confusion matrix" in r.text and "n = 30 matched traces" in r.text
    assert "77%" in r.text and "23 of 30 match" in r.text
    assert "0.46" in r.text and "moderate" in r.text
    assert re.search(r'<b>17</b><small>57%</small><span class="tag">agree</span>', r.text)
    assert "View as table" in r.text


def test_preset_is_default_and_picks_a_score_with_both_sources(client, login, world, db):
    add(db, world, "x1", "solo", "z1", "API", value=1.0)
    paired_bools(db, world, (17, 3, 4, 6))
    r = page(client, login, world)
    assert "Confusion matrix" in r.text and 'aria-pressed="true" data-preset' in r.text


def test_compare_not_enough_data(client, login, world, db):
    paired_bools(db, world, (3, 1, 1, 1))
    r = page(client, login, world, "?score=correct")
    assert "Not enough data." in r.text
    assert "Only 6 traces have both" in r.text
    assert "Confusion matrix" not in r.text


def test_numeric_compare_shows_scatter_and_correlation(client, login, world, db):
    for i in range(12):
        add(db, world, f"a{i}", "helpfulness", f"n{i}", "ANNOTATION", value=i / 11)
        add(db, world, f"b{i}", "relevance", f"n{i}", "EVAL", value=min(1.0, i / 11 + 0.05))
    db.commit()
    r = page(client, login, world, "?a=helpfulness&as=ANNOTATION&b=relevance&bs=EVAL")
    assert "data-an-chart=" in r.text and '"kind": "scatter"' in r.text
    assert "Pearson r" in r.text and "Spearman ρ" in r.text
    assert "12" in r.text


def test_mixed_compare_shows_box_plot(client, login, world, db):
    for i in range(12):
        add(
            db,
            world,
            f"a{i}",
            "tone",
            f"m{i}",
            "ANNOTATION",
            text="ok" if i % 2 else "great",
            kind="CATEGORICAL",
        )
        add(db, world, f"b{i}", "helpfulness", f"m{i}", "EVAL", value=0.5 + (i % 2) * 0.3)
    db.commit()
    r = page(client, login, world, "?a=tone&as=ANNOTATION&b=helpfulness&bs=EVAL")
    assert "Box plot of helpfulness per tone" in r.text
    assert "great · n 6" in r.text


def test_distribution_cards_and_not_enough_card(client, login, world, db):
    for i in range(20):
        add(db, world, f"h{i}", "helpfulness", f"d{i}", "ANNOTATION", value=i / 19)
    for i in range(6):
        add(db, world, f"c{i}", "tone", f"d{i}", "ANNOTATION", text="ok", kind="CATEGORICAL")
    db.commit()
    r = page(client, login, world)
    assert "Histogram of helpfulness in 10 bins" in r.text
    assert "numeric" in r.text and "categorical" in r.text
    assert "tone</span> has 6 scores. Statistics need at least 10." in r.text
    assert "Mean" in r.text and "p90" in r.text


def test_trend_by_source_with_table_fallback(client, login, world, db):
    for i in range(12):
        add(
            db,
            world,
            f"h{i}",
            "helpfulness",
            f"r{i}",
            "ANNOTATION",
            value=0.8,
            ago=timedelta(days=i % 3),
        )
        add(
            db, world, f"e{i}", "helpfulness", f"q{i}", "EVAL", value=0.6, ago=timedelta(days=i % 3)
        )
    db.commit()
    r = page(client, login, world, "?score=helpfulness")
    assert '"kind": "trend"' in r.text
    assert "Annotation mean" in r.text and "Eval mean" in r.text


def test_date_range_switch_changes_data(client, login, world, db):
    for i in range(12):
        add(db, world, f"o{i}", "old-score", f"o{i}", "API", value=0.5, ago=timedelta(days=5))
    db.commit()
    assert "No scores in this range" in page(client, login, world, "?range=24h").text
    r = page(client, login, world, "?range=7d")
    assert "old-score" in r.text and "No scores in this range" not in r.text


def test_public_feedback_toggle(client, login, world, db):
    for i in range(12):
        add(
            db,
            world,
            f"f{i}",
            "thumbs",
            f"f{i}",
            "API",
            value=1,
            kind="BOOLEAN",
            channel="public_feedback",
        )
    db.commit()
    assert "thumbs" in page(client, login, world).text
    assert "No scores in this range" in page(client, login, world, "?feedback=0").text


def test_environment_filter(client, login, world, db):
    for i in range(12):
        add(db, world, f"s{i}", "staging-only", f"s{i}", "API", value=0.5, env="staging")
    db.commit()
    assert "No scores in this range" in page(client, login, world, "?env=production").text
    assert "staging-only" in page(client, login, world, "?env=staging").text


def test_nav_links_to_the_page(client, login, world):
    r = page(client, login, world)
    assert f"/project/{pid(world)}/scores/analytics" in r.text


@pytest.mark.parametrize("role", list(Role))
def test_every_role_can_read(client, login, world, role):
    assert page(client, login, world, role=role).status_code == 200


def test_other_projects_scores_are_not_shown(client, login, world, db, make_project):
    other = make_project(world.org)
    add(db, world, "z", "foreign-score", "z", "API", value=1.0)
    db.query(Score).filter_by(id="z").update({"project_id": other.id})
    db.commit()
    assert "foreign-score" not in page(client, login, world).text
