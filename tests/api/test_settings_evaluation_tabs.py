from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import AuditLog, ScoreConfig
from traceloom.db.models_eval import EvalSpendDaily
from traceloom.ports import ScoreRow
from traceloom.scores import configs as score_configs
from traceloom.scores import feedback

XSS = "<script>alert(1)</script>"
ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"


def scores_url(world, suffix=""):
    return f"/project/{world.project.id}/settings/scores{suffix}"


def eval_url(world, suffix=""):
    return f"/project/{world.project.id}/settings/evaluation{suffix}"


def as_role(client, login, world, role):
    login(client, world.users[role])  # sets the X-CSRF-Token header, as app.js does
    return client


def post(client, url, **data):
    return client.post(url, data=data, follow_redirects=False)


def audits(db, action):
    db.expire_all()
    return list(db.scalars(select(AuditLog).where(AuditLog.action == action)))


def configs(db, world):
    db.expire_all()
    return {c.name: c for c in score_configs.list_configs(db, world.project.id, True)}


# --- score configs ------------------------------------------------------------------------------


def test_admin_creates_a_boolean_config_with_public_feedback(client, login, world, db):
    as_role(client, login, world, Role.ADMIN)
    r = post(client, scores_url(world), name="thumbs", data_type="BOOLEAN", public_feedback="1",
             description="Shown to end users")  # fmt: skip
    assert r.status_code == 303 and "notice=created" in r.headers["location"]
    config = configs(db, world)["thumbs"]
    assert (config.data_type, config.public_feedback, config.created_by) == (
        "BOOLEAN",
        True,
        world.users[Role.ADMIN].id,
    )
    assert audits(db, "score_config.create")[0].target == config.id
    page = client.get(scores_url(world)).text
    assert "thumbs" in page and "true, false" in page and "pill ok" in page


def test_numeric_and_categorical_configs_and_validation(client, login, world, db):
    as_role(client, login, world, Role.ADMIN)
    post(
        client, scores_url(world), name="quality", data_type="NUMERIC", min_value="0", max_value="1"
    )
    r = client.post(
        scores_url(world),
        data={"name": "tone", "data_type": "CATEGORICAL", "cat_label": ["poor", "great", ""],
              "cat_value": ["0", "1", ""]},
        follow_redirects=False,
    )  # fmt: skip
    assert r.status_code == 303
    found = configs(db, world)
    assert (found["quality"].min_value, found["quality"].max_value) == (0.0, 1.0)
    assert found["tone"].categories == [
        {"label": "poor", "value": 0.0},
        {"label": "great", "value": 1.0},
    ]
    dup = post(client, scores_url(world), name="quality", data_type="NUMERIC")
    assert dup.status_code == 400 and "already exists" in dup.text and "data-autoopen" in dup.text
    bad = post(client, scores_url(world), name="x", data_type="NUMERIC", min_value="abc")
    assert bad.status_code == 400 and "must be a number" in bad.text
    half = client.post(
        scores_url(world),
        data={"name": "y", "data_type": "CATEGORICAL", "cat_label": ["a"], "cat_value": [""]},
    )
    assert half.status_code == 400 and "Category 1 needs a label and a number" in half.text
    assert "y" not in configs(db, world)


def test_edit_allows_description_and_range_but_locks_type_once_used(
    client, login, world, db, container
):
    as_role(client, login, world, Role.ADMIN)
    config = score_configs.create_config(
        db, world.project.id, name="quality", data_type="NUMERIC", min_value=0, max_value=1
    )
    ok = post(client, scores_url(world, f"/{config.id}"), name="quality", data_type="NUMERIC",
              min_value="0", max_value="5", description="new")  # fmt: skip
    assert ok.status_code == 303
    assert configs(db, world)["quality"].max_value == 5.0
    assert audits(db, "score_config.update")
    container.trace_store.add_score(
        world.project.id,
        ScoreRow(id="s1", name="quality", timestamp=datetime.now(UTC), trace_id="t1", value=1.0,
                 config_id=config.id),
    )  # fmt: skip
    page = client.get(scores_url(world)).text
    assert "1 score use this config" in page
    locked = post(client, scores_url(world, f"/{config.id}"), name="quality", data_type="BOOLEAN")
    assert locked.status_code == 400 and "cannot change once scores use this config" in locked.text
    assert configs(db, world)["quality"].data_type == "NUMERIC"
    still = post(client, scores_url(world, f"/{config.id}"), name="quality", data_type="NUMERIC",
                 min_value="0", max_value="9", description="again")  # fmt: skip
    assert still.status_code == 303


def test_archive_and_unarchive_are_audited(client, login, world, db):
    as_role(client, login, world, Role.ADMIN)
    config = score_configs.create_config(db, world.project.id, name="thumbs", data_type="BOOLEAN")
    r = post(client, scores_url(world, f"/{config.id}/archive"))
    assert r.status_code == 303
    assert configs(db, world)["thumbs"].archived_at is not None
    assert "No active configs" in client.get(scores_url(world)).text
    archived = client.get(scores_url(world, "?show=archived")).text
    assert "thumbs" in archived and "Unarchive" in archived
    back = post(client, scores_url(world, f"/{config.id}/unarchive"))
    assert back.status_code == 303 and configs(db, world)["thumbs"].archived_at is None
    assert [a.action for a in audits(db, "score_config.archive")] and audits(
        db, "score_config.unarchive"
    )


def test_unarchive_conflict_is_reported(client, login, world, db):
    as_role(client, login, world, Role.ADMIN)
    old = score_configs.create_config(db, world.project.id, name="thumbs", data_type="BOOLEAN")
    score_configs.set_archived(db, world.project.id, old.id, True)
    score_configs.create_config(db, world.project.id, name="thumbs", data_type="BOOLEAN")
    r = post(client, scores_url(world, f"/{old.id}/unarchive"))
    assert r.status_code == 409 and "Another config named" in r.text


@pytest.mark.parametrize("role", [Role.MEMBER, Role.VIEWER])
def test_members_and_viewers_read_but_cannot_write(client, login, world, db, role):
    config = score_configs.create_config(
        db, world.project.id, name="quality", data_type="NUMERIC", description=XSS
    )
    as_role(client, login, world, role)
    page = client.get(scores_url(world))
    assert page.status_code == 200 and "quality" in page.text and "Read-only" in page.text
    assert "New score config" not in page.text and "data-dialog-open" not in page.text
    assert XSS not in page.text and ESCAPED in page.text
    for suffix, data in (
        ("", {"name": "z", "data_type": "BOOLEAN"}),
        (f"/{config.id}", {"name": "z", "data_type": "NUMERIC"}),
        (f"/{config.id}/archive", {}),
        (f"/{config.id}/unarchive", {}),
    ):
        assert post(client, scores_url(world, suffix), **data).status_code == 403
    assert set(configs(db, world)) == {"quality"}


def test_config_text_is_escaped_in_the_admin_page(client, login, world, db):
    score_configs.create_config(
        db, world.project.id, name=XSS, data_type="NUMERIC", description=XSS
    )
    as_role(client, login, world, Role.ADMIN)
    page = client.get(scores_url(world)).text
    assert XSS not in page and ESCAPED in page


def test_configs_are_project_scoped(client, login, world, db, make_org, make_project):
    other = make_project(make_org())
    theirs = score_configs.create_config(db, other.id, name="theirs", data_type="BOOLEAN")
    as_role(client, login, world, Role.ADMIN)
    assert "theirs" not in client.get(scores_url(world)).text
    assert post(client, scores_url(world, f"/{theirs.id}/archive")).status_code == 404
    assert (
        post(client, scores_url(world, f"/{theirs.id}"), name="x", data_type="BOOLEAN").status_code
        == 404
    )
    assert db.get(ScoreConfig, theirs.id).archived_at is None


# --- evaluation tab ---------------------------------------------------------------------


def test_admin_saves_the_cap_and_it_is_audited(client, login, world, db):
    as_role(client, login, world, Role.ADMIN)
    r = post(client, eval_url(world, "/cap"), cap="25.50")
    assert r.status_code == 303
    page = client.get(eval_url(world)).text
    assert "$25.50" in page and 'value="25.50"' in page
    (row,) = audits(db, "eval.cap.change")
    assert (row.detail["before"], row.detail["after"]) == (10.0, 25.5)
    reset = post(client, eval_url(world, "/cap"), cap="")
    assert reset.status_code == 303 and "$10.00" in client.get(eval_url(world)).text
    assert post(client, eval_url(world, "/cap"), cap="-1").status_code == 400
    assert post(client, eval_url(world, "/cap"), cap="lots").status_code == 400
    zero = post(client, eval_url(world, "/cap"), cap="0")
    assert zero.status_code == 303 and "every judge is paused" in client.get(eval_url(world)).text


def test_the_page_shows_todays_spend(client, login, world, db):
    db.add(
        EvalSpendDaily(project_id=world.project.id, day=datetime.now(UTC).date(), spent_usd=3.84)
    )
    db.commit()
    as_role(client, login, world, Role.ADMIN)
    page = client.get(eval_url(world)).text
    assert "$3.84" in page and "$6.16" in page and 'aria-valuenow="38"' in page


def test_origins_are_added_removed_and_audited(client, login, world, db):
    as_role(client, login, world, Role.ADMIN)
    assert (
        post(client, eval_url(world, "/origins"), origin="https://App.Example.com/").status_code
        == 303
    )
    assert feedback.feedback_origins(db, world.project.id) == ["https://app.example.com"]
    assert "https://app.example.com" in client.get(eval_url(world)).text
    bad = post(client, eval_url(world, "/origins"), origin="app.example/path")
    assert bad.status_code == 400 and "Use an origin only" in bad.text
    db.expire_all()
    assert feedback.feedback_origins(db, world.project.id) == ["https://app.example.com"]
    gone = post(client, eval_url(world, "/origins/remove"), origin="https://app.example.com")
    assert gone.status_code == 303
    db.expire_all()
    assert feedback.feedback_origins(db, world.project.id) == []
    assert (
        post(client, eval_url(world, "/origins/remove"), origin="https://nope.example").status_code
        == 404
    )
    assert len(audits(db, "feedback.origins.change")) == 2


def test_origin_text_is_escaped(client, login, world, db):
    as_role(client, login, world, Role.ADMIN)
    bad = post(client, eval_url(world, "/origins"), origin=XSS)
    assert bad.status_code == 400 and XSS not in bad.text and ESCAPED in bad.text


@pytest.mark.parametrize("role", [Role.MEMBER, Role.VIEWER])
def test_evaluation_tab_is_read_only_for_members_and_viewers(client, login, world, db, role):
    feedback.set_feedback_origins(db, world.project.id, ["https://app.example.com"])
    db.commit()
    as_role(client, login, world, role)
    page = client.get(eval_url(world))
    assert page.status_code == 200 and "https://app.example.com" in page.text
    assert (
        "Save cap" not in page.text
        and 'name="origin"' not in page.text
        and "Read-only" in page.text
    )
    assert post(client, eval_url(world, "/cap"), cap="1").status_code == 403
    assert post(client, eval_url(world, "/origins"), origin="https://x.example").status_code == 403
    assert (
        post(
            client, eval_url(world, "/origins/remove"), origin="https://app.example.com"
        ).status_code
        == 403
    )
    db.expire_all()
    assert feedback.feedback_origins(db, world.project.id) == ["https://app.example.com"]


def test_the_snippet_uses_a_public_key_and_the_static_file_is_small(client, login, world, make_key):
    _, public, _ = make_key(world.project)
    as_role(client, login, world, Role.ADMIN)
    page = client.get(eval_url(world)).text
    assert f"publicKey: &#34;{public}&#34;" in page or f'publicKey: "{public}"' in page
    assert "/static/feedback.js" in page and "data-traceloom-feedback" in page and "Copy" in page
    script = Path("traceloom/web/static/feedback.js")
    assert script.stat().st_size < 3 * 1024
    served = client.get("/static/feedback.js")
    assert served.status_code == 200 and "Traceloom" in served.text
    assert re.search(r"\b(import|require)\b\s*\(", served.text) is None  # no dependencies


def test_tabs_and_the_evaluation_nav_group(client, login, world):
    as_role(client, login, world, Role.VIEWER)
    page = client.get(scores_url(world)).text
    assert ">Score configs</a>" in page and ">Evaluation</a>" in page
    for label in ("Scores", "Annotation queues", "Evaluators", "Datasets"):
        assert f"</svg>{label}</a>" in page
    assert '<div class="cap">Evaluation</div>' in page


@pytest.mark.xfail(
    strict=True, reason="QA T-9: CSP default-src 'self' blocks the meter's inline width"
)
def test_qa_cap_meter_has_no_inline_style(client, login, world):
    # With style="width:N%" blocked by the CSP the meter fill defaults to full width, so a project
    # with $0 spent shows a 100% bar. The width has to come from a stylesheet or script.
    as_role(client, login, world, Role.ADMIN)
    html = client.get(eval_url(world), headers={"Accept": "text/html"}).text
    assert not re.search(r'<i style="width:', html)
