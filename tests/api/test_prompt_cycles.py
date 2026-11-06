"""Cycle detection runs against the state after the write, inside the same transaction (QA T-5)."""

from __future__ import annotations

import base64

import pytest
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import AuditLog, Prompt, PromptLabel, PromptVersion, WebhookDelivery
from traceloom.prompts import service
from traceloom.prompts.service import Actor, PromptError
from traceloom.webhooks import events

BASE = "/api/public/v2/prompts"


@pytest.fixture
def project(world):
    return world.project


@pytest.fixture
def admin(world):
    return Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)


def save(db, project, who, name, content, **kw):
    row = service.create_version(
        db, project, who, name=name, type="text", content=content, commit_message="m", **kw
    )
    db.commit()
    return row


def versions(db, name):
    return db.scalars(
        select(PromptVersion.version)
        .join(Prompt, Prompt.id == PromptVersion.prompt_id)
        .where(Prompt.name == name)
    ).all()


def assert_cycle(exc, *names):
    message = str(exc.value)
    assert "Circular" in message
    for name in names:
        assert name in message


# --- service ------------------------------------------------------------------------------


def test_save_closing_a_loop_through_latest_is_rejected(db, project, admin):
    save(db, project, admin, "cyc/a", "base a")
    save(db, project, admin, "cyc/b", "b uses @@@prompt:cyc/a|label=latest@@@")
    with pytest.raises(PromptError) as exc:
        save(db, project, admin, "cyc/a", "a uses @@@prompt:cyc/b|label=latest@@@")
    assert_cycle(exc, "cyc/a", "cyc/b")
    assert versions(db, "cyc/a") == [1]  # the new version was rolled back
    assert service.get(db, project, admin, "cyc/a").prompt == "base a"
    assert service.get(db, project, admin, "cyc/b").prompt == "b uses base a"


def test_rejected_save_leaves_no_audit_or_event(db, project, admin):
    from traceloom.db.models import Webhook

    db.add(
        Webhook(
            project_id=project.id,
            url="https://e.com/h",
            events=[events.VERSION_CREATED],
            enabled=True,
        )
    )
    db.commit()
    save(db, project, admin, "cyc/a", "base a")
    save(db, project, admin, "cyc/b", "b uses @@@prompt:cyc/a|label=latest@@@")
    audits = len(db.scalars(select(AuditLog)).all())
    sent = len(db.scalars(select(WebhookDelivery)).all())
    with pytest.raises(PromptError):
        save(db, project, admin, "cyc/a", "a uses @@@prompt:cyc/b|label=latest@@@")
    db.commit()
    assert len(db.scalars(select(AuditLog)).all()) == audits
    assert len(db.scalars(select(WebhookDelivery)).all()) == sent


def test_self_include_on_a_new_prompt_is_a_cycle_not_a_missing_reference(db, project, admin):
    with pytest.raises(PromptError) as exc:
        save(db, project, admin, "me", "I am @@@prompt:me|label=latest@@@")
    assert_cycle(exc, "me")
    assert "not found" not in str(exc.value)
    assert db.scalar(select(Prompt)) is None  # the prompt itself was not created


def test_self_include_on_a_new_version_is_a_cycle(db, project, admin):
    save(db, project, admin, "me", "plain")
    with pytest.raises(PromptError) as exc:
        save(db, project, admin, "me", "again @@@prompt:me|label=latest@@@")
    assert_cycle(exc, "me")
    assert versions(db, "me") == [1]


def test_self_include_by_version_number_is_a_cycle(db, project, admin):
    save(db, project, admin, "me", "plain")
    with pytest.raises(PromptError) as exc:
        save(db, project, admin, "me", "again @@@prompt:me|version=2@@@")
    assert_cycle(exc, "me")


def test_save_closing_a_loop_through_a_label_set_on_the_same_save(db, project, admin):
    save(db, project, admin, "a", "A", labels=["live"])
    save(db, project, admin, "b", "B @@@prompt:a|label=live@@@")
    with pytest.raises(PromptError) as exc:
        # a v2 is not `latest`-referenced by anyone, but labelling it `live` closes b -> a -> b
        save(db, project, admin, "a", "A2 @@@prompt:b|label=latest@@@", labels=["live"])
    assert_cycle(exc, "a", "b")
    assert versions(db, "a") == [1]
    assert (
        db.get(
            PromptLabel, (db.scalar(select(Prompt.id).where(Prompt.name == "a")), "live")
        ).version
        == 1
    )


def loop_ready(db, project, admin):
    """a v2 -> b -> a@production, with production still on a v1, so nothing is cyclic yet."""
    save(db, project, admin, "a", "A")
    service.move_label(db, project, admin, "a", "production", 1)
    save(db, project, admin, "b", "B @@@prompt:a|label=production@@@", labels=["live"])
    save(db, project, admin, "a", "A2 @@@prompt:b|label=live@@@")
    db.commit()


def test_label_move_closing_a_loop_is_rejected_and_not_applied(db, project, admin):
    loop_ready(db, project, admin)
    moves = len(db.scalars(select(AuditLog).where(AuditLog.action == "prompt.label_moved")).all())
    with pytest.raises(PromptError) as exc:
        service.move_label(db, project, admin, "a", "production", 2)
    assert_cycle(exc, "a", "b")
    db.commit()
    assert service.get(db, project, admin, "a", label="production").version == 1
    assert (
        len(db.scalars(select(AuditLog).where(AuditLog.action == "prompt.label_moved")).all())
        == moves
    )
    assert service.get(db, project, admin, "b").prompt == "B A"  # nothing is poisoned


def test_label_move_that_does_not_close_a_loop_still_works(db, project, admin):
    loop_ready(db, project, admin)
    assert service.move_label(db, project, admin, "b", "other", 1) is True
    assert service.move_label(db, project, admin, "a", "staging", 2) is True  # staging is unused


def test_label_move_to_a_self_including_version_is_a_cycle(db, project, admin):
    save(db, project, admin, "me", "plain", labels=["live"])
    save(db, project, admin, "me", "again")  # v2
    # The new version names `live` and takes `live` on save, so it includes itself.
    with pytest.raises(PromptError):
        save(db, project, admin, "me", "x @@@prompt:me|label=live@@@", labels=["live"])
    assert versions(db, "me") == [1, 2]


def test_longer_loops_are_found(db, project, admin):
    save(db, project, admin, "p1", "one")
    save(db, project, admin, "p2", "two @@@prompt:p1|label=latest@@@")
    save(db, project, admin, "p3", "three @@@prompt:p2|label=latest@@@")
    with pytest.raises(PromptError) as exc:
        save(db, project, admin, "p1", "one @@@prompt:p3|label=latest@@@")
    assert_cycle(exc, "p1", "p2", "p3")


def test_acyclic_diamonds_are_fine(db, project, admin):
    save(db, project, admin, "base", "B")
    save(db, project, admin, "l", "L @@@prompt:base|label=latest@@@")
    save(db, project, admin, "r", "R @@@prompt:base|label=latest@@@")
    row = save(db, project, admin, "top", "@@@prompt:l|label=latest@@@ @@@prompt:r|label=latest@@@")
    assert row.dependencies and {d["name"] for d in row.dependencies} == {"l", "r", "base"}
    assert service.get(db, project, admin, "top").prompt == "L B R B"


def test_dependencies_are_stored_on_the_new_version(db, project, admin):
    save(db, project, admin, "tone", "Be kind.", labels=["live"])
    row = save(db, project, admin, "main", "Rules: @@@prompt:tone|label=live@@@")
    assert row.dependencies == [{"name": "tone", "version": 1, "label": "live"}]


# --- API (QA's repro) -----------------------------------------------------------------------


def basic(public, secret):
    return {"Authorization": "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()}


@pytest.fixture
def auth(make_key, world, db):
    row, public, secret = make_key(world.project)
    row.may_move_protected_labels = True
    db.commit()
    return basic(public, secret)


def post(client, auth, name, prompt, **extra):
    body = {"name": name, "type": "text", "prompt": prompt, "commitMessage": "m", **extra}
    return client.post(BASE, json=body, headers=auth)


def test_api_cycle_via_latest_is_400_and_leaves_both_prompts_readable(client, auth):
    assert post(client, auth, "cyc/a", "base a").status_code == 201
    assert post(client, auth, "cyc/b", "b uses @@@prompt:cyc/a|label=latest@@@").status_code == 201
    r = post(client, auth, "cyc/a", "a uses @@@prompt:cyc/b|label=latest@@@")
    assert r.status_code == 400
    assert "Circular" in r.json()["error"]["message"]
    a = client.get(f"{BASE}/cyc/a", headers=auth)
    b = client.get(f"{BASE}/cyc/b", headers=auth)
    assert (a.status_code, a.json()["version"], a.json()["prompt"]) == (200, 1, "base a")
    assert (b.status_code, b.json()["prompt"]) == (200, "b uses base a")


def test_api_self_include_is_a_cycle(client, auth):
    r = post(client, auth, "me", "x @@@prompt:me|label=latest@@@")
    assert r.status_code == 400
    assert (
        "Circular" in r.json()["error"]["message"]
        and "not found" not in r.json()["error"]["message"]
    )
    assert client.get(f"{BASE}/me", headers=auth).status_code == 404


def test_api_patch_production_closing_a_loop_is_400(client, auth):
    assert post(client, auth, "a", "A").status_code == 201
    ok = client.patch(f"{BASE}/a/versions/1", json={"newLabels": ["production"]}, headers=auth)
    assert ok.status_code == 200
    assert (
        post(client, auth, "b", "B @@@prompt:a|label=production@@@", labels=["live"]).status_code
        == 201
    )
    assert post(client, auth, "a", "A2 @@@prompt:b|label=live@@@").status_code == 201
    r = client.patch(f"{BASE}/a/versions/2", json={"newLabels": ["production"]}, headers=auth)
    assert r.status_code == 400 and "Circular" in r.json()["error"]["message"]
    assert client.get(f"{BASE}/a", headers=auth).json()["version"] == 1  # production did not move
    assert client.get(f"{BASE}/b", headers=auth).status_code == 200
