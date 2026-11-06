from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceloom.auth.permissions import Role
from traceloom.db.models import AuditLog, Project, Prompt, PromptLabel, PromptVersion
from traceloom.prompts import service
from traceloom.prompts.service import Actor, NotAllowed, PromptError, PromptNotFound

CHAT = [
    {"role": "system", "content": "You help {{user}}."},
    {"role": "user", "content": "{{question}}"},
]


@pytest.fixture
def project(world) -> Project:
    return world.project


def actor(world, role: Role) -> Actor:
    return Actor.for_user(world.users[role], role)


@pytest.fixture
def member(world) -> Actor:
    return actor(world, Role.MEMBER)


@pytest.fixture
def admin(world) -> Actor:
    return actor(world, Role.ADMIN)


@pytest.fixture
def viewer(world) -> Actor:
    return actor(world, Role.VIEWER)


def save(db, project, who, name="support", content="Hello {{name}}", **kw):
    kw.setdefault("commit_message", "init")
    kw.setdefault("type", "text")
    row = service.create_version(db, project, who, name=name, content=content, **kw)
    db.commit()
    return row


# --- versions -------------------------------------------------------------------------------


def test_versions_increment_from_one(db: Session, project, member):
    assert [save(db, project, member, content=f"v{i}").version for i in range(1, 4)] == [1, 2, 3]
    assert [v.version for v in service.versions(db, project, member, "support")] == [3, 2, 1]


def test_earlier_versions_are_unchanged_by_later_saves(db, project, member):
    save(db, project, member, content="first", config={"model": "a"})
    save(db, project, member, content="second", config={"model": "b"})
    first = service.get(db, project, member, "support", version=1)
    assert (first.prompt, first.config) == ("first", {"model": "a"})
    assert not hasattr(service, "update_version") and not hasattr(service, "delete_version")


def test_chat_prompt_round_trip(db, project, member):
    save(db, project, member, name="chatty", type="chat", content=CHAT)
    view = service.get(db, project, member, "chatty", version=1)
    assert view.type == "chat" and view.prompt == CHAT


def test_type_is_fixed_after_first_version(db, project, member):
    save(db, project, member)
    with pytest.raises(PromptError, match="cannot change"):
        save(db, project, member, type="chat", content=CHAT)
    save(db, project, member, type=None, content="again")  # type may be omitted later


def test_new_prompt_needs_a_type(db, project, member):
    with pytest.raises(PromptError, match="type"):
        save(db, project, member, name="fresh", type=None)


@pytest.mark.parametrize("name", ["", "has space", "ünï", "a" * 201, "a|b", "a@b", "x\n"])
def test_invalid_names_rejected(db, project, member, name):
    with pytest.raises(PromptError, match="Prompt names"):
        save(db, project, member, name=name)
    assert db.scalar(select(Prompt)) is None


@pytest.mark.parametrize("name", ["a", "A_b-c.d", "support/tone/formal", "x" * 200, "1.2"])
def test_valid_names_accepted(db, project, member, name):
    assert save(db, project, member, name=name).version == 1


def test_commit_message_is_required(db, project, member):
    for blank in ("", "   "):
        with pytest.raises(PromptError, match="commit message"):
            save(db, project, member, commit_message=blank)


def test_content_and_config_validation(db, project, member):
    with pytest.raises(PromptError, match="string"):
        save(db, project, member, content=["x"])
    with pytest.raises(PromptError, match="at least one message"):
        save(db, project, member, name="c", type="chat", content=[])
    with pytest.raises(PromptError, match="role"):
        save(db, project, member, name="c", type="chat", content=[{"role": "bot", "content": "x"}])
    with pytest.raises(PromptError, match="object"):
        save(db, project, member, config=["x"])
    with pytest.raises(PromptError, match="valid JSON"):
        save(db, project, member, config={"x": object()})
    assert db.scalar(select(Prompt)) is None


def test_viewer_cannot_save(db, project, viewer):
    with pytest.raises(NotAllowed):
        save(db, project, viewer)
    assert db.scalar(select(AuditLog).where(AuditLog.action.like("prompt.%"))) is None


def test_save_is_audited(db, project, member):
    save(db, project, member)
    save(db, project, member)
    rows = db.scalars(
        select(AuditLog).where(AuditLog.action.like("prompt.%")).order_by(AuditLog.created_at)
    ).all()
    assert [r.action for r in rows] == ["prompt.created", "prompt.version_created"]
    assert rows[1].detail["version"] == 2 and rows[1].project_id == project.id
    assert rows[1].actor_id == member.id and rows[1].org_id == project.org_id


def test_tags_are_normalised(db, project, member):
    save(db, project, member, tags=[" a ", "b", "a"])
    assert service.get(db, project, member, "support").tags == ["a", "b"]
    with pytest.raises(PromptError):
        save(db, project, member, tags=[""])


# --- labels on save, `latest`, get ----------------------------------------------------------


def test_labels_on_create(db, project, member):
    save(db, project, member, labels=["staging", "qa"])
    view = service.get(db, project, member, "support", label="staging")
    assert sorted(view.labels) == ["latest", "qa", "staging"]


def test_latest_cannot_be_set_by_hand(db, project, member):
    save(db, project, member)
    with pytest.raises(PromptError, match="latest"):
        service.move_label(db, project, member, "support", "latest", 1)
    with pytest.raises(PromptError, match="latest"):
        save(db, project, member, labels=["latest"])
    assert db.scalar(select(PromptLabel)) is None


def test_latest_follows_the_newest_version(db, project, member):
    save(db, project, member, content="one")
    save(db, project, member, content="two")
    assert service.get(db, project, member, "support", label="latest").prompt == "two"
    assert "latest" in service.get(db, project, member, "support", version=2).labels
    assert "latest" not in service.get(db, project, member, "support", version=1).labels


def test_get_defaults_to_production_then_latest(db, project, admin):
    save(db, project, admin, content="one")
    save(db, project, admin, content="two")
    assert service.get(db, project, admin, "support").prompt == "two"  # no production: latest
    service.move_label(db, project, admin, "support", "production", 1)
    db.commit()
    assert service.get(db, project, admin, "support").prompt == "one"
    assert service.get(db, project, admin, "support", label="latest").prompt == "two"


def test_get_errors(db, project, member):
    save(db, project, member)
    with pytest.raises(PromptNotFound):
        service.get(db, project, member, "ghost")
    with pytest.raises(PromptNotFound, match="label"):
        service.get(db, project, member, "support", label="staging")
    with pytest.raises(PromptNotFound, match="version 9"):
        service.get(db, project, member, "support", version=9)
    with pytest.raises(PromptError, match="either"):
        service.get(db, project, member, "support", label="a", version=1)


def test_reads_are_project_scoped(db, project, member, make_project, world):
    save(db, project, member)
    other = make_project(world.org)
    with pytest.raises(PromptNotFound):
        service.get(db, other, member, "support")
    assert service.list_prompts(db, other, member).total == 0
    save(db, other, member, content="other project")  # same name is free in another project
    assert service.get(db, project, member, "support").prompt == "Hello {{name}}"


# --- composition -------------------------------------------------------------------------


def test_composition_resolves_on_get_and_records_dependencies(db, project, member):
    save(db, project, member, name="tone", content="Be kind.", labels=["live"])
    save(db, project, member, name="main", content="Rules: @@@prompt:tone|label=live@@@ Go.")
    resolved = service.get(db, project, member, "main")
    assert resolved.prompt == "Rules: Be kind. Go."
    assert resolved.dependencies == [{"name": "tone", "version": 1, "label": "live"}]
    raw = service.get(db, project, member, "main", resolve=False)
    assert raw.prompt.startswith("Rules: @@@prompt:tone")
    assert raw.dependencies == resolved.dependencies  # recorded at save


def test_saving_a_missing_reference_is_rejected(db, project, member):
    with pytest.raises(PromptError, match="not found"):
        save(db, project, member, content="@@@prompt:ghost|label=x@@@")


def test_saving_a_loop_is_rejected(db, project, member):
    save(db, project, member, name="a", content="A", labels=["x"])
    save(db, project, member, name="b", content="B @@@prompt:a|label=x@@@", labels=["x"])
    with pytest.raises(PromptError, match="Circular"):
        save(db, project, member, name="a", content="A @@@prompt:b|label=x@@@", labels=["x"])
    assert service.get(db, project, member, "a", label="x").prompt == "A"


def test_get_still_reports_a_loop_already_in_the_data(db, project, member):
    """Data written before the save-time check (or by hand) must not hang or 500 a read."""
    save(db, project, member, name="a", content="A", labels=["x"])
    save(db, project, member, name="b", content="B @@@prompt:a|label=x@@@", labels=["x"])
    prompt = db.scalar(select(Prompt).where(Prompt.name == "a"))
    db.add(
        PromptVersion(
            project_id=project.id,
            prompt_id=prompt.id,
            version=2,
            content="A @@@prompt:b|label=x@@@",
            commit_message="legacy",
        )
    )
    db.get(PromptLabel, (prompt.id, "x")).version = 2  # a@x -> v2 -> b -> a@x
    db.commit()
    with pytest.raises(PromptError, match="Circular"):
        service.get(db, project, member, "a", label="x")


# --- list ---------------------------------------------------------------------------------


@pytest.fixture
def library(db, project, member):
    save(
        db,
        project,
        member,
        name="support/greeting",
        content="Hello there",
        tags=["cs"],
        labels=["staging"],
    )
    save(
        db,
        project,
        member,
        name="support/refund",
        content="Refund policy text",
        tags=["cs", "legal"],
    )
    save(db, project, member, name="support/deep/escalate", content="Escalate", tags=[])
    save(db, project, member, name="sales-pitch", type="chat", content=CHAT, tags=["sales"])
    save(db, project, member, name="support/greeting", content="Hello again there")
    return project


def names(result):
    return [s.name for s in result.items]


def test_list_all_sorted_by_name(db, library, member):
    result = service.list_prompts(db, library, member)
    assert names(result) == [
        "sales-pitch",
        "support/deep/escalate",
        "support/greeting",
        "support/refund",
    ]
    greeting = result.items[2]
    assert greeting.latest_version == 2 and greeting.labels == {"staging": 1}


def test_list_folders(db, library, member):
    top = service.list_prompts(db, library, member, direct_only=True)
    assert names(top) == ["sales-pitch"]
    assert top.folders == [("support", 3)]
    inside = service.list_prompts(db, library, member, folder="support", direct_only=True)
    assert names(inside) == ["support/greeting", "support/refund"]
    assert inside.folders == [("support/deep", 1)]
    assert service.list_prompts(db, library, member, folder="supp").total == 0


def test_list_search_name_tag_and_content(db, library, member):
    assert names(service.list_prompts(db, library, member, search="REFUND")) == ["support/refund"]
    assert names(service.list_prompts(db, library, member, search="legal")) == ["support/refund"]
    assert names(service.list_prompts(db, library, member, search="again")) == ["support/greeting"]
    assert names(service.list_prompts(db, library, member, search="Hello there")) == []  # v1 text
    assert names(service.list_prompts(db, library, member, search="question")) == ["sales-pitch"]


def test_list_search_treats_wildcards_literally(db, library, member):
    assert service.list_prompts(db, library, member, search="%").total == 0
    assert service.list_prompts(db, library, member, folder="%").total == 0


def test_list_filters(db, library, member):
    assert names(service.list_prompts(db, library, member, tag="cs")) == [
        "support/greeting",
        "support/refund",
    ]
    assert names(service.list_prompts(db, library, member, label="staging")) == ["support/greeting"]
    assert service.list_prompts(db, library, member, label="latest").total == 4
    assert service.list_prompts(db, library, member, tag="cs", label="staging").total == 1


def test_list_sort_and_paging(db, library, member):
    by_updated = service.list_prompts(db, library, member, sort="updated", descending=True)
    assert names(by_updated)[0] == "support/greeting"  # saved last
    desc = service.list_prompts(db, library, member, descending=True)
    assert names(desc)[0] == "support/refund"
    page2 = service.list_prompts(db, library, member, limit=3, page=2)
    assert names(page2) == ["support/refund"] and page2.total == 4
    with pytest.raises(PromptError):
        service.list_prompts(db, library, member, sort="bogus")


# --- delete / protected -----------------------------------------------------------------------


def test_delete_needs_admin_and_removes_everything(db, project, member, admin):
    save(db, project, member, labels=["staging"])
    save(db, project, member)
    with pytest.raises(NotAllowed):
        service.delete(db, project, member, "support")
    service.delete(db, project, admin, "support")
    db.commit()
    assert db.scalar(select(Prompt)) is None
    assert db.scalar(select(PromptVersion)) is None and db.scalar(select(PromptLabel)) is None
    row = db.scalar(select(AuditLog).where(AuditLog.action == "prompt.deleted"))
    assert row.detail == {"prompt": "support", "versions": 2} and row.actor_id == admin.id
    with pytest.raises(PromptNotFound):
        service.delete(db, project, admin, "support")


def test_delete_is_project_scoped(db, project, member, admin, make_project, world):
    save(db, project, member)
    with pytest.raises(PromptNotFound):
        service.delete(db, make_project(world.org), admin, "support")
    assert db.scalar(select(Prompt)) is not None


# --- composition and size limits (review fixes) -----------------------------------------------


def _insert_fan_out(db, project, member, width=20, levels=5):
    """Prompts l1..l{levels}, each including the next one `width` times. Written straight to the
    tables, as a series of label moves could produce, since a save would refuse them."""
    save(db, project, member, name=f"l{levels}", content="leaf ")
    for level in range(levels - 1, 0, -1):
        prompt = Prompt(project_id=project.id, name=f"l{level}", type="text", created_by=member.id)
        db.add(prompt)
        db.flush()
        content = f"@@@prompt:l{level + 1}|label=latest@@@" * width
        db.add(PromptVersion(project_id=project.id, prompt_id=prompt.id, version=1,
                             content=content, commit_message="raw"))  # fmt: skip
    db.commit()


def test_fan_out_of_20_refs_by_5_levels_is_rejected_quickly_at_save(db, project, member):
    import time

    _insert_fan_out(db, project, member)
    started = time.monotonic()
    with pytest.raises(PromptError, match="includes"):
        save(db, project, member, name="root", content="@@@prompt:l1|label=latest@@@" * 20)
    assert time.monotonic() - started < 2
    db.rollback()
    with pytest.raises(PromptError, match="includes"):
        service.get(db, project, member, "l1", resolve=True)  # reads are capped as well


def test_normal_includes_still_save_and_resolve(db, project, member):
    save(db, project, member, name="greeting", content="Hello {{name}}. ")
    save(db, project, member, name="root", content="@@@prompt:greeting|label=latest@@@" * 3)
    view = service.get(db, project, member, "root", resolve=True)
    assert view.prompt == "Hello {{name}}. " * 3


def test_a_version_over_the_content_limit_is_refused(db, project, member):
    with pytest.raises(PromptError, match="characters"):
        save(db, project, member, content="x" * (service.MAX_CONTENT_CHARS + 1))
    db.rollback()
    big = [{"role": "user", "content": "x" * (service.MAX_CONTENT_CHARS // 2 + 1)}] * 2
    with pytest.raises(PromptError, match="characters"):
        save(db, project, member, name="chat", type="chat", content=big)


# --- races (review fixes) ---------------------------------------------------------------------


def test_label_move_racing_an_insert_becomes_an_update(db, project, member, monkeypatch):
    save(db, project, member, content="one")
    save(db, project, member, content="two")
    service.move_label(db, project, member, "support", "staging", 1)
    db.commit()
    real_get, stale = db.get, [True]

    def racing_get(model, key, *args, **kwargs):
        if model is PromptLabel and stale and stale.pop():
            return None  # read before the other request's insert committed
        return real_get(model, key, *args, **kwargs)

    monkeypatch.setattr(db, "get", racing_get)
    assert service.move_label(db, project, member, "support", "staging", 2) is True
    db.commit()
    monkeypatch.undo()
    assert service.get(db, project, member, "support", label="staging").version == 2


def test_a_save_that_keeps_losing_the_version_race_is_a_conflict(db, project, member, monkeypatch):
    save(db, project, member, content="one")
    monkeypatch.setattr(service, "_latest_version", lambda *a: 0)  # always races to v1
    with pytest.raises(service.PromptConflict, match="same time"):
        save(db, project, member, content="two")
