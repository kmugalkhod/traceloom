from __future__ import annotations

import pytest
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import (
    AuditLog,
    Project,
    Prompt,
    PromptLabel,
    ProtectedLabel,
    Webhook,
    WebhookDelivery,
)
from traceloom.prompts import service
from traceloom.prompts.service import Actor, NotAllowed, PromptError, PromptNotFound
from traceloom.webhooks import events


def actor(world, role: Role) -> Actor:
    return Actor.for_user(world.users[role], role)


@pytest.fixture
def project(world) -> Project:
    return world.project


@pytest.fixture
def member(world) -> Actor:
    return actor(world, Role.MEMBER)


@pytest.fixture
def admin(world) -> Actor:
    return actor(world, Role.ADMIN)


@pytest.fixture
def hook(db, project) -> Webhook:
    row = Webhook(
        project_id=project.id,
        url="https://example.com/hook",
        events=[events.LABEL_MOVED, events.VERSION_CREATED],
        enabled=True,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def prompt(db, project, member):
    for text in ("one", "two", "three"):
        service.create_version(
            db, project, member, name="support", type="text", content=text, commit_message=text
        )
    db.commit()


def label_audits(db):
    return db.scalars(select(AuditLog).where(AuditLog.action == "prompt.label_moved")).all()


def deliveries(db, event=None):
    stmt = select(WebhookDelivery)
    if event:
        stmt = stmt.where(WebhookDelivery.event == event)
    return db.scalars(stmt).all()


# --- AC-5: protected labels -----------------------------------------------------------------


def test_production_is_protected_by_default(db, project):
    assert service.protected_labels(db, project) == ["production"]


def test_member_cannot_move_production(db, project, prompt, member, hook):
    before = len(deliveries(db))
    with pytest.raises(NotAllowed, match="protected"):
        service.move_label(db, project, member, "support", "production", 2)
    db.commit()
    assert label_audits(db) == []
    assert len(deliveries(db)) == before  # nothing new
    assert db.scalar(select(PromptLabel)) is None


def test_member_cannot_remove_a_protected_label(db, project, prompt, member, admin):
    service.move_label(db, project, admin, "support", "production", 1)
    db.commit()
    with pytest.raises(NotAllowed):
        service.remove_label(db, project, member, "support", "production")
    assert service.get(db, project, member, "support", label="production").version == 1


def test_member_cannot_set_a_protected_label_while_saving(db, project, member):
    with pytest.raises(NotAllowed):
        service.create_version(
            db,
            project,
            member,
            name="p",
            type="text",
            content="x",
            commit_message="m",
            labels=["production"],
        )
    db.rollback()
    assert db.scalar(select(Prompt)) is None  # nothing saved, not even the version


def test_member_moves_staging_and_it_is_audited(db, project, prompt, member):
    assert service.move_label(db, project, member, "support", "staging", 2) is True
    db.commit()
    assert service.get(db, project, member, "support", label="staging").version == 2
    (row,) = label_audits(db)
    assert row.actor_type == "user" and row.actor_id == member.id
    assert row.detail == {
        "prompt": "support",
        "label": "staging",
        "from_version": None,
        "to_version": 2,
    }
    service.move_label(db, project, member, "support", "staging", 3)
    db.commit()
    assert [r.detail["from_version"] for r in label_audits(db)] == [None, 2]


def test_admin_moves_production(db, project, prompt, admin):
    service.move_label(db, project, admin, "support", "production", 2)
    db.commit()
    assert service.get(db, project, admin, "support").version == 2
    assert label_audits(db)[0].detail["label"] == "production"


def test_moving_to_the_same_version_is_a_no_op(db, project, prompt, member, hook):
    service.move_label(db, project, member, "support", "staging", 2)
    db.commit()
    audits, sent = len(label_audits(db)), len(deliveries(db, events.LABEL_MOVED))
    assert service.move_label(db, project, member, "support", "staging", 2) is False
    assert (len(label_audits(db)), len(deliveries(db, events.LABEL_MOVED))) == (audits, sent)


def test_remove_label(db, project, prompt, member):
    service.move_label(db, project, member, "support", "staging", 1)
    assert service.remove_label(db, project, member, "support", "staging") is True
    assert service.remove_label(db, project, member, "support", "staging") is False
    db.commit()
    assert [r.detail["to_version"] for r in label_audits(db)] == [1, None]
    with pytest.raises(PromptNotFound):
        service.get(db, project, member, "support", label="staging")


def test_move_errors(db, project, prompt, member):
    with pytest.raises(PromptNotFound, match="version 9"):
        service.move_label(db, project, member, "support", "staging", 9)
    with pytest.raises(PromptNotFound):
        service.move_label(db, project, member, "ghost", "staging", 1)
    with pytest.raises(PromptError):
        service.move_label(db, project, member, "support", "Bad Label", 1)
    with pytest.raises(PromptError):
        service.move_label(db, project, member, "support", "x" * 37, 1)


def test_viewer_cannot_move_labels(db, project, prompt, world):
    with pytest.raises(NotAllowed):
        service.move_label(db, project, actor(world, Role.VIEWER), "support", "staging", 1)


def test_labels_do_not_cross_projects(db, project, prompt, member, admin, make_project, world):
    other = make_project(world.org)
    with pytest.raises(PromptNotFound):
        service.move_label(db, other, admin, "support", "staging", 1)
    assert db.scalar(select(PromptLabel)) is None


def test_set_protected(db, project, prompt, member, admin):
    with pytest.raises(NotAllowed):
        service.set_protected(db, project, member, "staging", True)
    assert service.set_protected(db, project, admin, "staging", True) is True
    assert service.set_protected(db, project, admin, "staging", True) is False
    db.commit()
    assert service.protected_labels(db, project) == ["production", "staging"]
    with pytest.raises(NotAllowed):
        service.move_label(db, project, member, "support", "staging", 1)
    service.move_label(db, project, admin, "support", "staging", 1)

    assert service.set_protected(db, project, admin, "staging", False) is True
    service.move_label(db, project, member, "support", "staging", 2)  # open to Members again
    db.commit()
    audits = db.scalars(
        select(AuditLog).where(AuditLog.action == "prompt.protected_label_changed")
    ).all()
    assert [a.detail for a in audits] == [
        {"label": "staging", "protected": True},
        {"label": "staging", "protected": False},
    ]


def test_set_protected_validates_and_is_project_scoped(db, project, admin, make_project, world):
    with pytest.raises(PromptError):
        service.set_protected(db, project, admin, "latest", True)
    other = make_project(world.org)
    service.set_protected(db, project, admin, "release", True)
    db.commit()
    assert "release" not in service.protected_labels(db, other)
    assert db.get(ProtectedLabel, (other.id, "production")) is not None


# --- API-key actor -----------------------------------------------------------------------


def test_key_needs_protected_flag(db, project, prompt):
    plain = Actor.for_key("k1")
    flagged = Actor.for_key("k2", may_move_protected=True)
    service.move_label(db, project, plain, "support", "staging", 1)
    with pytest.raises(NotAllowed, match="API key"):
        service.move_label(db, project, plain, "support", "production", 1)
    service.move_label(db, project, flagged, "support", "production", 1)
    db.commit()
    assert [(r.actor_type, r.actor_id) for r in label_audits(db)] == [
        ("api_key", "k1"),
        ("api_key", "k2"),
    ]
    with pytest.raises(NotAllowed):
        service.delete(db, project, flagged, "support")
    with pytest.raises(NotAllowed):
        service.set_protected(db, project, plain, "x", True)


# --- webhook events ---------------------------------------------------------------------------


def test_label_move_enqueues_pending_delivery(db, project, prompt, member, hook):
    service.move_label(db, project, member, "support", "staging", 2)
    db.commit()
    (row,) = deliveries(db, events.LABEL_MOVED)
    assert (row.webhook_id, row.project_id, row.status, row.attempts) == (
        hook.id,
        project.id,
        "pending",
        0,
    )
    assert row.next_attempt_at is not None
    assert row.payload["id"] == row.id and row.payload["event"] == events.LABEL_MOVED
    assert row.payload["project_id"] == project.id and row.payload["created_at"]
    assert row.payload["data"] == {
        "prompt": "support",
        "label": "staging",
        "from_version": None,
        "to_version": 2,
        "actor": member.name,
    }


def test_version_created_event(db, project, member, hook):
    service.create_version(
        db, project, member, name="p", type="text", content="x", commit_message="m"
    )
    db.commit()
    (row,) = deliveries(db, events.VERSION_CREATED)
    assert row.payload["data"] == {"prompt": "p", "version": 1, "actor": member.name}


def test_events_only_for_enabled_and_subscribed_webhooks(db, project, prompt, member, hook):
    db.add_all(
        [
            Webhook(
                project_id=project.id,
                url="https://e.com/off",
                events=[events.LABEL_MOVED],
                enabled=False,
            ),
            Webhook(
                project_id=project.id,
                url="https://e.com/other",
                events=[events.VERSION_CREATED],
                enabled=True,
            ),
            Webhook(project_id=project.id, url="https://e.com/none", events=[], enabled=True),
        ]
    )
    db.commit()
    service.move_label(db, project, member, "support", "staging", 1)
    db.commit()
    rows = deliveries(db, events.LABEL_MOVED)
    assert [r.webhook_id for r in rows] == [hook.id]


def test_other_projects_webhooks_get_nothing(db, project, prompt, member, make_project, world):
    other = make_project(world.org)
    db.add(
        Webhook(
            project_id=other.id, url="https://e.com/x", events=[events.LABEL_MOVED], enabled=True
        )
    )
    db.commit()
    service.move_label(db, project, member, "support", "staging", 1)
    db.commit()
    assert deliveries(db, events.LABEL_MOVED) == []


def test_no_webhooks_means_no_deliveries(db, project, prompt, member):
    service.move_label(db, project, member, "support", "staging", 1)
    db.commit()
    assert deliveries(db) == []


def test_label_move_survives_a_webhook_failure(db, project, prompt, member, hook, monkeypatch):
    original = db.add

    def boom(obj, *a, **kw):
        if isinstance(obj, WebhookDelivery):
            raise RuntimeError("queue down")
        return original(obj, *a, **kw)

    monkeypatch.setattr(db, "add", boom)
    assert service.move_label(db, project, member, "support", "staging", 2) is True
    monkeypatch.undo()
    db.commit()
    assert service.get(db, project, member, "support", label="staging").version == 2
    assert len(label_audits(db)) == 1
    assert deliveries(db, events.LABEL_MOVED) == []


def test_enqueue_returns_rows_and_never_raises(db, project, hook):
    rows = events.enqueue(db, project.id, events.LABEL_MOVED, {"x": 1})
    assert len(rows) == 1
    assert events.enqueue(db, project.id, "unknown.event", {}) == []
