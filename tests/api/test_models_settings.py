from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import AuditLog, ModelDefinition

H = {"Accept": "text/html"}
NOW = datetime.now(UTC)


def url(world, suffix=""):
    return f"/project/{world.project.id}/settings/models{suffix}"


def form(**over):
    data = {
        "model_name": "Sonnet override",
        "match_pattern": "^claude-sonnet-4-5$",
        "price_input": "1.00",
        "price_output": "2.00",
        "price_cache_read": "",
        "price_cache_write": "",
        "valid_from": "",
        "tokenizer": "",
    }
    return {**data, **over}


def generation_cost(client, container, world, key_headers, model="claude-sonnet-4-5"):
    batch = {
        "batch": [
            {
                "id": "e1",
                "type": "generation-create",
                "timestamp": NOW.isoformat(),
                "body": {
                    "id": "g1",
                    "traceId": "t1",
                    "model": model,
                    "startTime": NOW.isoformat(),
                    "usageDetails": {"input": 100_000, "output": 100_000},
                },
            }
        ]
    }
    assert client.post("/api/public/ingestion", json=batch, headers=key_headers).status_code == 207
    container.queue.drain()
    obs = container.trace_store.get_existing(world.project.id, {"observation": ["g1"]})[
        "observation"
    ]["g1"]
    return obs.cost_details.get("total"), obs.cost_source


@pytest.fixture
def key_headers(world, make_key):
    _, public, secret = make_key(world.project)
    return {"Authorization": "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()}


@pytest.mark.parametrize("role", [Role.VIEWER, Role.MEMBER])
def test_members_and_viewers_get_403(client, world, login, role):
    login(client, world.user(role))
    assert client.get(url(world), headers=H).status_code == 403
    assert client.post(url(world), data=form()).status_code == 403
    assert client.post(url(world, "/x"), data=form()).status_code == 403
    assert client.post(url(world, "/x/delete")).status_code == 403


def test_list_shows_builtin_and_project_definitions(client, world, login):
    login(client, world.user(Role.ADMIN))
    page = client.get(url(world), headers=H).text
    assert "claude-sonnet-4-5" in page and "Built-in" in page and "Override" in page
    assert "$3" in page and "$15" in page  # per 1M tokens, from the seed
    assert "Add model" in page


def test_project_definition_is_saved_edited_deleted_and_overrides_the_builtin(
    client, container, world, login, db, key_headers
):
    login(client, world.user(Role.ADMIN))
    cost, source = generation_cost(client, container, world, key_headers)
    assert (round(cost, 6), source) == (1.8, "calculated")  # built-in $3 + $15 per 1M tokens

    r = client.post(url(world), data=form(), follow_redirects=False)
    assert r.status_code == 303 and "notice=saved" in r.headers["location"]
    row = db.scalar(select(ModelDefinition).where(ModelDefinition.project_id == world.project.id))
    assert row.prices == {"input": 1e-6, "output": 2e-6} and row.source == "user"
    page = client.get(url(world, "?notice=saved"), headers=H).text
    assert (
        "Sonnet override" in page
        and "overrides the built-in price" in page
        and "Model definition saved." in page
    )

    # Editing: the form is pre-filled in USD per 1M tokens, and the new price takes effect.
    edit = client.get(url(world, f"?edit={row.id}"), headers=H).text
    assert 'value="1"' in edit and 'value="2"' in edit and "Edit Sonnet override" in edit
    assert (
        client.post(
            url(world, f"/{row.id}"),
            data=form(price_input="4", price_output="8"),
            follow_redirects=False,
        ).status_code
        == 303
    )
    cost, source = generation_cost(client, container, world, key_headers)
    assert (
        round(cost, 6) == 1.2 and source == "calculated"
    )  # the project price, not the built-in one

    assert client.post(url(world, f"/{row.id}/delete"), follow_redirects=False).status_code == 303
    cost, _ = generation_cost(client, container, world, key_headers)
    assert round(cost, 6) == 1.8  # back to the built-in price
    actions = [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.created_at))]
    assert [a for a in actions if a.startswith("model_definition")] == [
        "model_definition.created",
        "model_definition.updated",
        "model_definition.deleted",
    ]


def test_override_starts_from_the_builtin_values_with_tiers(client, world, login, db):
    login(client, world.user(Role.ADMIN))
    gemini = db.scalar(
        select(ModelDefinition).where(ModelDefinition.model_name == "gemini-2.5-pro")
    )
    page = client.get(url(world, f"?override={gemini.id}"), headers=H).text
    assert 'value="gemini-2.5-pro"' in page and 'value="1.25"' in page and 'value="200000"' in page
    assert 'value="2.5"' in page and 'value="15"' in page  # the tier's prices
    posted = client.post(
        url(world),
        data={
            **form(
                model_name="gemini-2.5-pro",
                match_pattern="^gemini-2.5-pro$",
                price_input="1.25",
                price_output="10",
            ),
            "tier_threshold": ["200000", ""],
            "tier_input": ["2.5", ""],
            "tier_output": ["15", ""],
            "tier_cache_read": ["", ""],
            "tier_cache_write": ["", ""],
        },
        follow_redirects=False,
    )
    assert posted.status_code == 303
    row = db.scalar(select(ModelDefinition).where(ModelDefinition.project_id == world.project.id))
    assert row.tiers == [{"threshold": 200000, "prices": {"input": 2.5e-6, "output": 1.5e-5}}]


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"match_pattern": "("}, "not a valid regex"),
        ({"model_name": " "}, "model_name is required"),
        ({"price_input": "-1"}, "zero or more"),
        ({"price_input": "abc"}, "must be a number"),
        ({"price_input": "", "price_output": ""}, "non-empty map"),
        ({"valid_from": "yesterday"}, "date like"),
    ],
)
def test_invalid_definitions_are_rejected_inline(client, world, login, db, over, message):
    login(client, world.user(Role.ADMIN))
    r = client.post(url(world), data=form(**over))
    assert r.status_code == 400 and message in r.text and 'role="alert"' in r.text
    assert (
        db.scalar(select(ModelDefinition).where(ModelDefinition.project_id == world.project.id))
        is None
    )


def test_bad_tier_is_rejected(client, world, login):
    login(client, world.user(Role.ADMIN))
    r = client.post(
        url(world),
        data={
            **form(),
            "tier_threshold": ["lots"],
            "tier_input": ["1"],
            "tier_output": ["1"],
            "tier_cache_read": [""],
            "tier_cache_write": [""],
        },
    )
    assert r.status_code == 400 and "whole number" in r.text


def test_builtin_and_other_projects_definitions_cannot_be_changed(
    client, world, login, make_project, db
):
    other = make_project(world.org)
    foreign = ModelDefinition(
        project_id=other.id, model_name="theirs", match_pattern="^t$", prices={"input": 1.0}
    )
    db.add(foreign)
    db.commit()
    builtin = db.scalar(select(ModelDefinition).where(ModelDefinition.project_id.is_(None)))
    login(client, world.user(Role.OWNER))
    for definition in (foreign, builtin):
        assert client.get(url(world, f"?edit={definition.id}"), headers=H).status_code == 404
        assert client.post(url(world, f"/{definition.id}"), data=form()).status_code == 404
        assert client.post(url(world, f"/{definition.id}/delete")).status_code == 404
    assert "theirs" not in client.get(url(world), headers=H).text
    db.expire_all()
    assert (
        db.get(ModelDefinition, foreign.id) is not None
        and db.get(ModelDefinition, builtin.id) is not None
    )


def test_a_bad_form_for_another_projects_definition_is_404_without_its_details(
    client, world, login, make_project, db
):
    other = make_project(world.org)
    foreign = ModelDefinition(
        project_id=other.id,
        model_name="their-secret-model",
        match_pattern="^their-secret-pattern$",
        prices={"input": 1.0},
    )
    db.add(foreign)
    db.commit()
    builtin = db.scalar(select(ModelDefinition).where(ModelDefinition.project_id.is_(None)))
    login(client, world.user(Role.OWNER))
    for definition in (foreign, builtin):
        r = client.post(url(world, f"/{definition.id}"), data=form(price_input="lots"))
        assert r.status_code == 404
        assert definition.model_name not in r.text and definition.match_pattern not in r.text
    assert client.post(url(world, "/no-such-id"), data=form(price_input="lots")).status_code == 404


def test_definition_names_are_escaped(client, world, login):
    login(client, world.user(Role.ADMIN))
    client.post(url(world), data=form(model_name="<script>alert(1)</script>"))
    html = client.get(url(world), headers=H).text
    assert (
        "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    )


def test_models_tab_is_only_offered_to_admins(client, world, login):
    for role, shown in ((Role.VIEWER, False), (Role.ADMIN, True)):
        login(client, world.user(role))
        tabs = (
            client.get(f"/project/{world.project.id}/settings", headers=H)
            .text.split('role="tablist"')[1]
            .split("</div>")[0]
        )
        assert ("Models" in tabs) is shown
