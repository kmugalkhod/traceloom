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

    db.add(Webhook(project_id=project.id, url="https://e.com/h", events=[events.VERSION_CREATED], enabled=True))
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
    assert db.get(PromptLabel, (db.scalar(select(Prompt.id).where(Prompt.name == "a")), "live")).version == 1


def test_label_move_closing_a_loop_is_rejected_and_not_applied(db, project, admin):
    save(db, project, admin, "a", "A")
    save(db, project, admin, "b", "B @@@prompt:a|label=production@@@", labels=[])  # needs production on a
    service.move_label(db, project, admin, "a", "production", 1)
    db.commit()
    save(db, project, admin, "b", "B @@@prompt:a|label=production@@@")  # b v1 failed above? ensure b exists
