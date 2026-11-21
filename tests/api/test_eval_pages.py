from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from tests.api.test_ingestion import auth, event
from tests.api.test_ingestion import post as ingest_post
from traceloom.auth.permissions import Role
from traceloom.db.models import AuditLog
from traceloom.db.models_eval import EvalExecution, EvalRule, EvalTemplate, EvalTemplateVersion
from traceloom.evals import spend
from traceloom.llm import connections
from traceloom.llm.connections import ConnectionPolicy
from traceloom.prompts.service import Actor

HTML = {"Accept": "text/html"}
XSS = "<script>alert(1)</script>"


def url(world, suffix=""):
    return f"/project/{world.project.id}/evals{suffix}"


def csrf(client, world):
    page = client.get(url(world, "/templates"), headers=HTML)
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def post(client, world, suffix, **data):
    data["csrf_token"] = csrf(client, world)
    return client.post(url(world, suffix), data=data, headers=HTML, follow_redirects=False)


def as_(client, login, world, role):
    return login(client, world.users[role])


@pytest.fixture
def connection(db, world, container):
    admin = Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)
    row = connections.create(
        db,
        world.project,
        admin,
        ConnectionPolicy.from_settings(container.settings),
        provider="openai",
        name="OpenAI main",
        secrets={"api_key": "sk-test-0000"},
    )
    db.commit()
    return row


def builtin(db, name="relevance"):
    return db.scalar(
        select(EvalTemplate).where(EvalTemplate.project_id.is_(None), EvalTemplate.name == name)
    )


def audits(db, prefix):
    db.expire_all()
    return [
        a.action
        for a in db.scalars(select(AuditLog).order_by(AuditLog.created_at, AuditLog.id))
        if a.action.startswith(prefix)
    ]


TEMPLATE = {
    "name": "tone-check",
    "prompt": "Rate {{output}} for tone",
    "type": "numeric",
    "min": "0",
    "max": "1",
    "temperature": "0",
    "max_tokens": "300",
}


# --- templates -----------------------------------------------------------------------------


def test_templates_page_lists_builtins_badged_with_clone(client, login, world, db):
    as_(client, login, world, Role.MEMBER)
    page = client.get(url(world, "/templates"), headers=HTML)
    assert page.status_code == 200
    for name in (
        "Hallucination",
        "Relevance",
        "Toxicity",
        "Helpfulness",
        "Correctness",
        "Conciseness",
    ):
        assert name in page.text
    assert page.text.count("Built-in") >= 6 and "Clone" in page.text and "{{input}}" in page.text


def test_viewer_reads_templates_but_cannot_write(client, login, world, db):
    as_(client, login, world, Role.VIEWER)
    page = client.get(url(world, "/templates"), headers=HTML)
    assert (
        page.status_code == 200 and "New template" not in page.text and ">Clone<" not in page.text
    )
    assert client.get(url(world, "/templates/new"), headers=HTML).status_code == 403
    assert post(client, world, "/templates", **TEMPLATE).status_code == 403


def test_member_clones_a_builtin_then_edits_it_into_new_versions(client, login, world, db):
    as_(client, login, world, Role.MEMBER)
    source = builtin(db)
    response = post(client, world, f"/templates/{source.id}/clone")
    assert response.status_code == 303
    copy_id = response.headers["location"].rsplit("/", 1)[1]
    db.expire_all()
    copy = db.get(EvalTemplate, copy_id)
    assert copy.project_id == world.project.id and copy.name == "relevance (copy)"
    editor = client.get(url(world, f"/templates/{copy_id}"), headers=HTML)
    assert "Save as v2" in editor.text and "{{input}}" in editor.text
    saved = post(
        client,
        world,
        f"/templates/{copy_id}",
        **{**TEMPLATE, "name": "ignored", "prompt": "New {{output}}"},
    )
    assert saved.status_code == 303
    db.expire_all()
    versions = db.scalars(
        select(EvalTemplateVersion)
        .where(EvalTemplateVersion.template_id == copy_id)
        .order_by(EvalTemplateVersion.version)
    ).all()
    assert [v.version for v in versions] == [1, 2] and versions[1].prompt == "New {{output}}"
    assert audits(db, "eval_template") == ["eval_template.clone", "eval_template.version"]
    again = post(client, world, f"/templates/{source.id}/clone")  # name taken: numbered
    db.expire_all()
    assert (
        db.get(EvalTemplate, again.headers["location"].rsplit("/", 1)[1]).name
        == "relevance (copy 2)"
    )


def test_builtin_page_is_read_only_and_cannot_be_saved(client, login, world, db):
    as_(client, login, world, Role.MEMBER)
    source = builtin(db)
    page = client.get(url(world, f"/templates/{source.id}"), headers=HTML)
    assert (
        "Built-in templates are read-only" in page.text
        and "Clone to edit" in page.text
        and "Save as" not in page.text
    )
    assert post(client, world, f"/templates/{source.id}", **TEMPLATE).status_code == 403


def test_member_creates_a_template_and_errors_show(client, login, world, db):
    as_(client, login, world, Role.MEMBER)
    bad = post(client, world, "/templates", **{**TEMPLATE, "min": "5", "max": "1"})
    assert bad.status_code == 400 and "min below max" in bad.text
    ok = post(client, world, "/templates", **TEMPLATE)
    assert ok.status_code == 303
    listing = client.get(url(world, "/templates"), headers=HTML).text
    assert "tone-check" in listing and "v1" in listing
    assert audits(db, "eval_template") == ["eval_template.create"]


def test_user_content_renders_escaped(client, login, world, db):
    as_(client, login, world, Role.MEMBER)
    post(
        client, world, "/templates", **{**TEMPLATE, "name": XSS, "prompt": f"{XSS} {{{{output}}}}"}
    )
    listing = client.get(url(world, "/templates"), headers=HTML).text
    assert XSS not in listing and "&lt;script&gt;alert(1)&lt;/script&gt;" in listing
    template = db.scalar(select(EvalTemplate).where(EvalTemplate.name == XSS))
    editor = client.get(url(world, f"/templates/{template.id}"), headers=HTML).text
    assert XSS not in editor


# --- rules ---------------------------------------------------------------------------------


RULE = {
    "name": "Chat relevance",
    "target": "trace",
    "trace_name": "chat",
    "sampling_pct": "100",
    "delay_seconds": "0",
    "score_name": "relevance",
}


def make_rule(client, world, db, **over):
    source = builtin(db)
    data = {**RULE, "template_id": source.id, **over}
    return post(client, world, "/rules", **data)


def test_member_gets_403_on_every_rule_control(client, login, world, db, make_eval_rule):
    rule = make_eval_rule(world.project, name="r", enabled=False)
    as_(client, login, world, Role.MEMBER)
    page = client.get(url(world, "/rules"), headers=HTML)
    assert (
        page.status_code == 200
        and "New rule" not in page.text
        and "Backfill" not in page.text
        and 'role="switch"' not in page.text
    )
    for method, suffix in [
        ("get", "/rules/new"),
        ("get", f"/rules/{rule.id}"),
        ("get", "/rules/preview"),
    ]:
        assert getattr(client, method)(url(world, suffix), headers=HTML).status_code == 403
    for suffix in [
        "/rules",
        f"/rules/{rule.id}",
        f"/rules/{rule.id}/toggle",
        f"/rules/{rule.id}/backfill",
        f"/rules/{rule.id}/delete",
    ]:
        assert post(client, world, suffix, enabled="1", confirm="1").status_code == 403
    db.expire_all()
    assert db.get(EvalRule, rule.id).enabled is False


def test_admin_creates_a_rule_confirms_the_provider_and_it_judges(
    client, login, world, db, container, connection, make_key
):
    as_(client, login, world, Role.ADMIN)
    created = make_rule(client, world, db, connection_id=connection.id, model="gpt-4o-mini")
    assert created.status_code == 303 and "confirm=" in created.headers["location"]
    rule = db.scalar(select(EvalRule))
    assert (
        rule.enabled is False and rule.filter == {"name": "chat"} and rule.template_version is None
    )

    page = client.get(created.headers["location"], headers=HTML)
    assert "Enable “Chat relevance”?" in page.text and "data-autoopen" in page.text
    assert (
        "<b>OpenAI</b>" in page.text
        and "<b>gpt-4o-mini</b>" in page.text
        and "OpenAI main" in page.text
    )
    assert (
        post(client, world, f"/rules/{rule.id}/toggle", enabled="1").status_code == 303
    )  # unconfirmed
    db.expire_all()
    assert db.get(EvalRule, rule.id).enabled is False
    assert (
        "Confirm that trace content"
        in client.get(
            url(world, "/rules?error=Confirm%20that%20trace%20content"), headers=HTML
        ).text
    )

    assert (
        post(client, world, f"/rules/{rule.id}/toggle", enabled="1", confirm="1").status_code == 303
    )
    db.expire_all()
    assert db.get(EvalRule, rule.id).enabled is True

    _, public, secret = make_key(world.project)
    events = [
        event(f"e{i}", "trace-create", {"id": f"t{i}", "name": "chat", "input": "q"})
        for i in range(3)
    ]
    assert ingest_post(client, auth(public, secret), events).status_code == 207
    container.queue.drain()
    db.expire_all()
    assert db.query(EvalExecution).filter_by(rule_id=rule.id).count() == 3
    assert audits(db, "eval_rule") == ["eval_rule.create", "eval_rule.enable"]


def test_enabling_needs_a_model(client, login, world, db):
    as_(client, login, world, Role.ADMIN)
    bad = make_rule(client, world, db)  # a built-in with no model and none chosen
    assert bad.status_code == 400 and "Choose a connection and model" in bad.text


def test_disable_and_delete_are_audited(client, login, world, db, make_eval_rule):
    rule_id = make_eval_rule(world.project, name="r", enabled=True).id
    as_(client, login, world, Role.ADMIN)
    assert post(client, world, f"/rules/{rule_id}/toggle", enabled="0").status_code == 303
    assert post(client, world, f"/rules/{rule_id}/delete").status_code == 303
    db.expire_all()
    assert db.get(EvalRule, rule_id) is None
    assert audits(db, "eval_rule") == ["eval_rule.disable", "eval_rule.delete"]


def test_rule_edit_saves_changes_and_audits(
    client, login, world, db, make_eval_rule, make_eval_template
):
    template = make_eval_template(
        world.project,
        "judge",
        prompt="{{input}} {{output}}",
        model="gpt-4o-mini",
        connection_id=None,
    )
    rule = make_eval_rule(world.project, template, name="r")
    as_(client, login, world, Role.ADMIN)
    page = client.get(url(world, f"/rules/{rule.id}"), headers=HTML)
    assert page.status_code == 200 and "map_input_source" in page.text
    saved = post(
        client,
        world,
        f"/rules/{rule.id}",
        template_id=template.id,
        name="renamed",
        target="observation",
        observation_name="answer",
        observation_type="generation",
        sampling_pct="25",
        delay_seconds="5",
        f_field=["tags", "metadata"],
        f_value=["a, b", "tier=pro"],
        map_input_source="metadata",
        map_input_key="q",
        map_input_default="n/a",
        map_input_path="$.a[-1]",
    )
    assert saved.status_code == 303
    db.expire_all()
    row = db.get(EvalRule, rule.id)
    assert (
        row.name,
        row.target,
        row.target_name,
        row.target_type,
        row.sampling_pct,
        row.delay_seconds,
    ) == ("renamed", "observation", "answer", "GENERATION", 25.0, 5)
    assert row.filter == {"tags": ["a", "b"], "metadata": {"tier": "pro"}}
    assert row.mapping == {"input": {"source": "metadata.q", "path": "$.a[-1]", "default": "n/a"}}
    assert audits(db, "eval_rule") == ["eval_rule.update"]


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"sampling_pct": "150"}, "Sampling must be between"),
        ({"delay_seconds": "-1"}, "Delay must be between"),
        (
            {"target": "observation", "observation_name": "", "observation_type": ""},
            "Name the observation",
        ),
        ({"map_input_path": "$..x", "map_input_source": "input"}, "path"),
        ({"template_id": "nope"}, "Choose a template"),
    ],
)
def test_invalid_rules_show_an_error_and_save_nothing(
    client, login, world, db, connection, over, message
):
    as_(client, login, world, Role.ADMIN)
    response = make_rule(
        client, world, db, connection_id=connection.id, model="gpt-4o-mini", **over
    )
    assert response.status_code == 400 and message in response.text
    assert db.query(EvalRule).count() == 0


def test_cap_banner_shows_while_the_cap_is_reached(
    client, login, world, db, make_eval_rule, settings
):
    make_eval_rule(world.project, name="r")
    as_(client, login, world, Role.ADMIN)
    assert "Daily cost cap of" not in client.get(url(world, "/rules"), headers=HTML).text
    spend.set_cap(db, world.project.id, 0)
    db.commit()
    page = client.get(url(world, "/rules"), headers=HTML)
    assert "Judges are paused" in page.text and "Change cap" in page.text
    spend.set_cap(db, world.project.id, 0.5)
    db.commit()
    now = datetime.now(UTC)
    from traceloom.db.models_eval import EvalSpendDaily

    db.add(EvalSpendDaily(project_id=world.project.id, day=now.date(), spent_usd=0.5))
    db.commit()
    page = client.get(url(world, "/rules"), headers=HTML)
    assert "Daily cost cap of $0.50 reached" in page.text
    member = login(client, world.users[Role.MEMBER])
    assert "Change cap" not in member.get(url(world, "/rules"), headers=HTML).text


def test_rule_text_is_escaped_and_outcomes_show(
    client, login, world, db, make_eval_rule, make_eval_execution
):
    rule = make_eval_rule(world.project, name=XSS, filter={"name": XSS}, enabled=True)
    make_eval_execution(rule, "t1", status="succeeded")
    make_eval_execution(rule, "t2", status="failed")
    as_(client, login, world, Role.ADMIN)
    page = client.get(url(world, "/rules"), headers=HTML).text
    assert XSS not in page and "&lt;script&gt;" in page
    assert "succeeded" in page and "failed" in page


def test_preview_resolves_variables_for_a_recent_match(
    client, login, world, db, container, make_eval_template, make_key
):
    template = make_eval_template(
        world.project, "judge", prompt="Q {{input}} A {{output}} T {{tier}}", model="m"
    )
    as_(client, login, world, Role.ADMIN)
    _, public, secret = make_key(world.project)
    ingest_post(
        client,
        auth(public, secret),
        [
            event(
                "e1",
                "trace-create",
                {
                    "id": "t1",
                    "name": "chat",
                    "input": {"messages": [{"content": "hi"}]},
                    "output": "hello",
                },
            )
        ],
    )
    container.queue.drain()
    query = {
        "template_id": template.id,
        "target": "trace",
        "trace_name": "chat",
        "map_input_source": "input",
        "map_input_path": "$.messages[-1].content",
        "map_tier_source": "metadata",
        "map_tier_key": "tier",
        "map_tier_default": "free",
    }
    page = client.get(url(world, "/rules/preview"), params=query, headers=HTML)
    assert (
        page.status_code == 200
        and "t1" in page.text
        and "hi" in page.text
        and "hello" in page.text
        and "free" in page.text
    )
    none = client.get(
        url(world, "/rules/preview"), params={**query, "trace_name": "other"}, headers=HTML
    )
    assert "No recent trace matches" in none.text


def test_backfill_queues_executions_and_is_audited(
    client, login, world, db, container, make_eval_rule, make_key
):
    rule = make_eval_rule(world.project, name="r", delay_seconds=0, enabled=False)
    _, public, secret = make_key(world.project)
    as_(client, login, world, Role.ADMIN)
    ingest_post(
        client,
        auth(public, secret),
        [event(f"e{i}", "trace-create", {"id": f"t{i}", "name": "x"}) for i in range(4)],
    )
    container.queue.drain()
    db.expire_all()
    assert db.query(EvalExecution).count() == 0  # a disabled rule matches nothing at ingest
    response = post(client, world, f"/rules/{rule.id}/backfill", **{"from": "", "to": ""})
    assert response.status_code == 303 and "Backfill+queued+4" in response.headers[
        "location"
    ].replace("%20", "+")
    db.expire_all()
    assert db.query(EvalExecution).count() == 4
    assert audits(db, "eval_rule") == ["eval_rule.backfill"]
    estimate = client.get(url(world, f"/rules/{rule.id}/backfill-estimate"))
    assert estimate.status_code == 200 and estimate.json()["matches"] == 4


def test_rules_are_project_scoped(client, login, world, db, make_org, make_project, make_eval_rule):
    other = make_eval_rule(make_project(make_org()), name="theirs")
    as_(client, login, world, Role.ADMIN)
    assert client.get(url(world, f"/rules/{other.id}"), headers=HTML).status_code == 404
    assert post(client, world, f"/rules/{other.id}/delete").status_code == 404


def test_evaluator_pages_use_no_inline_styles_or_handlers(
    client, login, world, db, make_eval_rule, make_eval_template
):
    """The app CSP is default-src 'self', which drops style="" and on*= attributes."""
    template = make_eval_template(world.project, "judge", prompt="{{input}} {{output}}", model="m")
    rule = make_eval_rule(world.project, template, name="r")
    as_(client, login, world, Role.ADMIN)
    paths = [
        "/templates",
        "/templates/new",
        f"/templates/{template.id}",
        "/rules",
        "/rules/new",
        f"/rules/{rule.id}",
        "/rules/preview",
    ]
    for path in paths:
        html = client.get(url(world, path), headers=HTML).text
        assert not re.search(r"<[^>]+\sstyle=", html), path
        assert not re.search(r"\son[a-z]+=", html), path


# --- rule filter: operators and score conditions (FR-J3 follow-up) ----------------------------


def _edit_filter(client, world, rule, template, **filters):
    return post(
        client,
        world,
        f"/rules/{rule.id}",
        template_id=template.id,
        name="r",
        target="trace",
        sampling_pct="100",
        delay_seconds="0",
        **filters,
    )


def test_rule_filter_stores_score_conditions_in_the_list_filter_format(
    client, login, world, db, make_eval_rule, make_eval_template
):
    template = make_eval_template(world.project, "judge", prompt="{{input}}", model="m")
    rule = make_eval_rule(world.project, template, name="r")
    as_(client, login, world, Role.ADMIN)
    saved = _edit_filter(
        client,
        world,
        rule,
        template,
        f_field=["tags", "score:quality", "score:thumbs"],
        f_op=["in", ">=", "missing"],
        f_value=["vip", "0.5", ""],
    )
    assert saved.status_code == 303
    db.expire_all()
    assert db.get(EvalRule, rule.id).filter == {
        "tags": ["vip"],
        "scores": ["quality:>=:0.5", "thumbs:missing"],
    }
    page = client.get(url(world, f"/rules/{rule.id}"), headers=HTML).text
    assert 'name="f_op"' in page
    assert '<option value="score:quality" selected>' in page
    assert '<option value="&gt;=" selected>' in page
    assert '<option value="missing" selected>' in page


def test_rule_filter_rejects_an_invalid_score_condition(
    client, login, world, db, make_eval_rule, make_eval_template
):
    template = make_eval_template(world.project, "judge", prompt="{{input}}", model="m")
    rule = make_eval_rule(world.project, template, name="r", filter={"name": "chat"})
    as_(client, login, world, Role.ADMIN)
    response = _edit_filter(
        client, world, rule, template, f_field=["score:quality"], f_op=[">"], f_value=["abc"]
    )
    assert response.status_code == 400 and "needs a number" in response.text
    db.expire_all()
    assert db.get(EvalRule, rule.id).filter == {"name": "chat"}


def test_rule_filter_offers_active_score_configs_and_escapes_names(
    client, login, world, db, make_eval_rule, make_eval_template
):
    from traceloom.db.models import ScoreConfig

    db.add(ScoreConfig(project_id=world.project.id, name=XSS, data_type="BOOLEAN"))
    db.commit()
    template = make_eval_template(world.project, "judge", prompt="{{input}}", model="m")
    rule = make_eval_rule(world.project, template, name="r")
    as_(client, login, world, Role.ADMIN)
    page = client.get(url(world, f"/rules/{rule.id}"), headers=HTML).text
    assert "score: &lt;script&gt;alert(1)&lt;/script&gt;" in page and XSS not in page
    assert "Same conditions as the trace filter" in page


def test_templates_page_features_three_starters_and_lists_the_rest(client, login, world):
    as_(client, login, world, Role.MEMBER)
    page = client.get(url(world, "/templates"), headers=HTML).text
    assert "Also built in:" in page
    featured = page.split("Also built in:")[0]
    for name in ("Hallucination", "Relevance", "Correctness"):
        assert name in featured
    for name in ("Toxicity", "Helpfulness", "Conciseness"):
        assert name not in featured
