"""Config-driven score inputs, chips and viewer read-only behaviour (T-10)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.helpers_web import post_form, seed_trace
from traceloom.auth.permissions import Role
from traceloom.ports import ScoreRow

HX = {"HX-Request": "true"}
BASE = "/project/{pid}/traces/{tid}"


@pytest.fixture
def trace(container, world):
    seed_trace(container, world.project.id, "t1")


def url(world, tid="t1"):
    return BASE.format(pid=world.project.id, tid=tid)


def discussion(client, world, tid="t1"):
    r = client.get(url(world, tid) + "/discussion")
    assert r.status_code == 200, r.text
    return r.text


def as_role(client, login, world, role):
    return login(client, world.user(role), csrf=False)


@pytest.fixture
def configs(world, make_score_config, db):
    made = {
        "quality": make_score_config(world.project, "quality"),
        "tone": make_score_config(
            world.project,
            "tone",
            data_type="CATEGORICAL",
            categories=[{"label": "polite", "value": 1}],
        ),
        "ok": make_score_config(world.project, "ok", data_type="BOOLEAN"),
        "retired": make_score_config(world.project, "retired"),
    }
    made["retired"].archived_at = datetime.now(UTC)
    db.commit()
    return made


def test_inputs_offer_active_configs_only_and_viewer_gets_none(
    client, login, world, trace, configs
):
    as_role(client, login, world, Role.MEMBER)
    html = discussion(client, world)
    for name in ("quality", "tone", "ok"):
        assert f'aria-label="{name}"' in html
    assert 'aria-label="retired"' not in html
    assert '<option value="polite">' in html and '<option value="true">' in html
    as_role(client, login, world, Role.VIEWER)
    html = discussion(client, world)
    assert "data-score-form" not in html and "<form" not in html


def test_config_input_validates_and_creates_annotation_score(
    client, login, world, trace, configs, container
):
    as_role(client, login, world, Role.MEMBER)
    html = discussion(client, world)

    def send(config_id, value):
        return post_form(
            client,
            html,
            url(world) + "/scores",
            {"config_id": config_id, "value": value},
            headers=HX,
        )

    assert send(configs["quality"].id, "7").status_code == 400  # above max 1
    assert send(configs["tone"].id, "rude").status_code == 400
    assert send(configs["retired"].id, "0.5").status_code == 400  # archived
    assert send("nope", "0.5").status_code == 400
    assert container.trace_store.get_trace(world.project.id, "t1").scores == []
    assert send(configs["quality"].id, "0.75").status_code == 200
    assert send(configs["tone"].id, "polite").status_code == 200
    assert send(configs["ok"].id, "true").status_code == 200
    got = {s.name: s for s in container.trace_store.get_trace(world.project.id, "t1").scores}
    assert got["quality"].value == 0.75 and got["quality"].config_id == configs["quality"].id
    assert got["quality"].source == "ANNOTATION" and got["quality"].author_id
    assert got["tone"].string_value == "polite" and got["ok"].value == 1.0


def test_viewer_cannot_post_a_score(client, login, world, trace, configs):
    as_role(client, login, world, Role.VIEWER)
    r = client.post(url(world) + "/scores", data={"config_id": configs["quality"].id, "value": "1"})
    assert r.status_code in (401, 403)


def test_chips_are_coloured_by_source_and_escaped(client, login, world, container):
    now = datetime.now(UTC)
    seed_trace(
        container,
        world.project.id,
        "t2",
        scores=[
            ScoreRow(
                id="a", trace_id="t2", name="<b>x</b>", value=1, timestamp=now,
                source="ANNOTATION", author_id="u", comment="<i>c</i>",
            ),
            ScoreRow(
                id="e", trace_id="t2", name="judge", value=0.5, timestamp=now,
                source="EVAL", execution_id="x",
            ),
        ],
    )  # fmt: skip
    as_role(client, login, world, Role.MEMBER)
    html = discussion(client, world, "t2")
    assert '<span class="sc ann' in html and '<span class="sc eval' in html
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html
    assert "<i>c</i>" not in html
