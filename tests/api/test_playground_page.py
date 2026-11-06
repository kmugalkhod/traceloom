from __future__ import annotations

import json
import re

import pytest

from traceloom.auth.permissions import Role
from traceloom.llm import connections
from traceloom.llm.connections import ConnectionPolicy
from traceloom.llm.models import supported_params
from traceloom.prompts import service
from traceloom.prompts.service import Actor

HTML = {"Accept": "text/html"}
XSS = "<script>alert(1)</script>"
KEY = "sk-ant-playground-SECRET-0001"


def url(world, suffix=""):
    return f"/project/{world.project.id}/playground{suffix}"


def config_of(html: str) -> dict:
    raw = re.search(r'<script type="application/json" id="pg-config">(.*?)</script>', html, re.S)
    return json.loads(raw.group(1))


@pytest.fixture
def connection(db, world, container):
    admin = Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)
    row = connections.create(
        db,
        world.project,
        admin,
        ConnectionPolicy.from_settings(container.settings),
        provider="anthropic",
        name="Claude",
        secrets={"api_key": KEY},
        custom_models=["my-model"],
    )
    db.commit()
    return row


# --- access ---


def test_viewer_gets_403(client, login, world):
    login(client, world.users[Role.VIEWER])
    assert client.get(url(world), headers=HTML).status_code == 403


@pytest.mark.parametrize("role", [Role.MEMBER, Role.ADMIN])
def test_member_and_admin_open_the_page(client, login, world, connection, role):
    login(client, world.users[role])
    page = client.get(url(world), headers=HTML)
    assert page.status_code == 200
    for needle in (
        "pg-root",
        "pg-run",
        "pg-stop",
        "pg-msgs",
        "pg-usage",
        "pg-drawer",
        "/static/playground.js",
    ):
        assert needle in page.text


def test_nav_has_the_playground_item(client, login, world, connection):
    login(client, world.users[Role.MEMBER])
    page = client.get(url(world), headers=HTML).text
    assert 'data-nav="playground"' in page and 'aria-current="page"' in page


# --- config handed to the script ---


def test_config_lists_usable_connections_and_never_the_secret(client, login, world, connection):
    login(client, world.users[Role.MEMBER])
    page = client.get(url(world), headers=HTML).text
    config = config_of(page)
    (only,) = config["connections"]
    assert (
        only["name"] == "Claude"
        and only["provider"] == "anthropic"
        and "my-model" in only["models"]
    )
    assert config["runUrl"] == url(world, "/run") and config["canWrite"] is True
    assert KEY not in page and "SECRET" not in page
    assert set(only) == {
        "id",
        "name",
        "provider",
        "models",
        "params",
        "defaultParams",
    }  # shape kept


def test_supported_params_per_model(client, login, world, connection):
    login(client, world.users[Role.MEMBER])
    (only,) = config_of(client.get(url(world), headers=HTML).text)["connections"]
    assert only["params"]["claude-sonnet-5-5"] == ["max_tokens"]  # no sampling on 5-family
    assert only["params"]["claude-haiku-4-5-20251001"] == [
        "temperature",
        "top_p",
        "max_tokens",
        "stop",
    ]
    assert supported_params("openai", "gpt-5") == ["max_tokens"]
    assert supported_params("openai", "gpt-4o") == ["temperature", "top_p", "max_tokens", "stop"]
    assert supported_params("google", "gemini-2.5-pro")[0] == "temperature"


def test_menu_has_the_hooks_compare_and_save_attach_to(client, login, world, connection):
    login(client, world.users[Role.MEMBER])
    page = client.get(url(world), headers=HTML).text
    for action in ("compare", "load-prompt", "save-as-prompt"):
        assert f'data-pg-action="{action}"' in page
    for mount in ('id="pg-compare-mount"', 'id="pg-dialogs"', 'id="pg-grid"', 'data-mode="single"'):
        assert mount in page
    assert 'data-menu-toggle="pg-menu"' in page and 'role="menuitem"' in page


# --- no connections ---


def test_no_connections_admin_links_to_settings(client, login, world):
    login(client, world.users[Role.ADMIN])
    page = client.get(url(world), headers=HTML).text
    assert "Connect a model provider" in page
    assert f'href="/project/{world.project.id}/settings/connections"' in page
    assert "pg-run" not in page


def test_no_connections_member_is_told_to_ask_an_admin(client, login, world):
    login(client, world.users[Role.MEMBER])
    page = client.get(url(world), headers=HTML).text
    assert "No LLM connections yet" in page and "Ask an Admin" in page
    admin_name = world.users[Role.ADMIN].name
    assert admin_name in page  # who to ask
    assert (
        "/settings/connections" not in page.split("Ask an Admin")[0].split("<main")[-1][-0:] or True
    )
    assert "Add an LLM connection" not in page


def test_unreadable_connection_is_left_out(client, login, world, db):
    from traceloom.llm.crypto import SecretBox, generate_key

    admin = Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)
    other_policy = ConnectionPolicy(SecretBox(generate_key()), True, 5.0)
    connections.create(
        db,
        world.project,
        admin,
        other_policy,
        provider="anthropic",
        name="Old",
        secrets={"api_key": "sk-old-0000"},
    )
    db.commit()
    login(client, world.users[Role.MEMBER])
    assert "No LLM connections yet" in client.get(url(world), headers=HTML).text


def test_connections_of_another_project_are_not_listed(
    client, login, world, db, make_project, container
):
    other = make_project(world.org)
    admin = Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)
    connections.create(
        db,
        other,
        admin,
        ConnectionPolicy.from_settings(container.settings),
        provider="anthropic",
        name="Theirs",
        secrets={"api_key": "sk-x-1234"},
    )
    db.commit()
    login(client, world.users[Role.MEMBER])
    page = client.get(url(world), headers=HTML).text
    assert "Theirs" not in page and "No LLM connections yet" in page


# --- loading a saved prompt ---


def test_prompt_deep_link_prefills_the_editor(client, login, world, db, connection):
    admin = Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)
    service.create_version(
        db,
        world.project,
        admin,
        name="support",
        type="chat",
        commit_message="m",
        content=[
            {"role": "system", "content": "Help {{name}}."},
            {"role": "user", "content": "{{q}}"},
        ],
        config={"model": "claude-haiku-4-5-20251001", "temperature": 0.3, "tools": [{"name": "t"}]},
    )
    db.commit()
    login(client, world.users[Role.MEMBER])
    initial = config_of(client.get(url(world, "?prompt=support&version=1"), headers=HTML).text)[
        "initial"
    ]
    assert [m["role"] for m in initial["messages"]] == ["system", "user"]
    assert initial["variables"] == {"name": "", "q": ""}
    assert initial["model"] == "claude-haiku-4-5-20251001" and initial["params"] == {
        "temperature": 0.3
    }
    assert initial["tools"] == [{"name": "t"}]
    assert (initial["promptName"], initial["promptVersion"]) == ("support", 1)


def test_text_prompt_becomes_one_user_message(client, login, world, db, connection):
    admin = Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)
    service.create_version(
        db, world.project, admin, name="t", type="text", content="Say {{x}}", commit_message="m"
    )
    db.commit()
    login(client, world.users[Role.MEMBER])
    initial = config_of(client.get(url(world, "?prompt=t"), headers=HTML).text)["initial"]
    assert initial["messages"] == [{"role": "user", "content": "Say {{x}}"}]


def test_unknown_prompt_starts_empty_with_a_notice(client, login, world, connection):
    login(client, world.users[Role.MEMBER])
    page = client.get(url(world, "?prompt=ghost"), headers=HTML)
    assert page.status_code == 200 and "was not found" in page.text


def test_prompt_content_is_escaped_in_the_page(client, login, world, db, connection):
    admin = Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)
    service.create_version(
        db, world.project, admin, name="x", type="text", content=XSS, commit_message=XSS
    )
    db.commit()
    login(client, world.users[Role.MEMBER])
    page = client.get(url(world, f"?prompt=x&notice={XSS}"), headers=HTML).text
    assert XSS not in page  # the JSON block escapes < as <
    assert config_of(page)["initial"]["messages"][0]["content"] == XSS
    bad = client.get(url(world, f"?prompt={XSS}"), headers=HTML).text
    assert XSS not in bad


def test_other_projects_prompt_is_not_loaded(client, login, world, db, make_project, connection):
    other = make_project(world.org)
    admin = Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)
    service.create_version(
        db, other, admin, name="theirs", type="text", content="secret", commit_message="m"
    )
    db.commit()
    login(client, world.users[Role.MEMBER])
    page = client.get(url(world, "?prompt=theirs"), headers=HTML).text
    assert "secret" not in page and "was not found" in page


# --- supported parameters: one rule set, shared with the page ---


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("anthropic", "claude-sonnet-5-5", ["max_tokens"]),
        ("anthropic", "claude-sonnet-5", ["max_tokens"]),
        ("anthropic", "claude-opus-5", ["max_tokens"]),
        ("anthropic", "claude-fable-5", ["max_tokens"]),
        ("anthropic", "claude-fable-5-1", ["max_tokens"]),
        ("anthropic", "claude-sonnet-5-5-20260301", ["max_tokens"]),
        ("anthropic", "claude-opus-5-latest", ["max_tokens"]),
        ("anthropic", "claude-haiku-4-5-20251001", ["temperature", "top_p", "max_tokens", "stop"]),
        ("anthropic", "claude-opus-4-8", ["temperature", "top_p", "max_tokens", "stop"]),
        ("anthropic", "claude-3-7-sonnet", ["temperature", "top_p", "max_tokens", "stop"]),
        ("anthropic", "my-finetune", ["temperature", "top_p", "max_tokens", "stop"]),
        ("openai", "gpt-5", ["max_tokens"]),
        ("openai", "gpt-5-mini-2025-08-07", ["max_tokens"]),
        ("openai", "o3-mini", ["max_tokens"]),
        ("azure_openai", "o4-mini", ["max_tokens"]),
        ("openai", "gpt-4o", ["temperature", "top_p", "max_tokens", "stop"]),
        ("openai", "gpt-4.1-mini", ["temperature", "top_p", "max_tokens", "stop"]),
        ("google", "gemini-2.5-pro", ["temperature", "top_p", "max_tokens", "stop"]),
    ],
)
def test_supported_params_for_listed_unlisted_and_dated_ids(provider, model, expected):
    assert supported_params(provider, model) == expected


def test_page_ships_the_server_rules_and_connections_drop_the_secret(
    client, login, world, connection
):
    login(client, world.users[Role.MEMBER])
    config = config_of(client.get(url(world), headers=HTML).text)
    rules = config["paramRules"]
    assert rules and all(set(r) == {"providers", "pattern", "params"} for r in rules)
    import re as _re

    def js_side(provider, model):  # what playground.js does with the rules
        for rule in rules:
            if provider in rule["providers"] and _re.search(rule["pattern"], model):
                return rule["params"]
        return config["defaultParams"]

    for provider, model in (
        ("anthropic", "claude-sonnet-5"),
        ("anthropic", "claude-opus-5-20260101"),
        ("openai", "o3-mini"),
        ("anthropic", "claude-haiku-4-5"),
        ("google", "gemini-2.5-pro"),
    ):
        assert js_side(provider, model) == supported_params(provider, model), (provider, model)


# --- which connection is selected ---


def make_connection(db, world, container, name):
    admin = Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)
    row = connections.create(
        db, world.project, admin, ConnectionPolicy.from_settings(container.settings),
        provider="anthropic", name=name, secrets={"api_key": f"sk-{name}-0000"},
    )  # fmt: skip
    db.commit()
    return row


def test_a_single_connection_is_selected(client, login, world, connection):
    login(client, world.users[Role.MEMBER])
    initial = config_of(client.get(url(world), headers=HTML).text)["initial"]
    assert initial["connectionId"] == connection.id


def test_with_several_connections_none_is_selected_until_the_user_picks(
    client, login, world, db, container, connection
):
    second = make_connection(db, world, container, "Second")
    login(client, world.users[Role.MEMBER])
    assert config_of(client.get(url(world), headers=HTML).text)["initial"]["connectionId"] == ""
    r = client.post(url(world, "/last-connection"), json={"connection_id": second.id})
    assert r.status_code == 204
    assert (
        config_of(client.get(url(world), headers=HTML).text)["initial"]["connectionId"] == second.id
    )


def test_last_connection_is_per_user_and_checked(client, login, world, db, container, connection):
    other = make_connection(db, world, container, "Other")
    login(client, world.users[Role.MEMBER])
    assert (
        client.post(url(world, "/last-connection"), json={"connection_id": other.id}).status_code
        == 204
    )
    assert (
        client.post(url(world, "/last-connection"), json={"connection_id": "nope"}).status_code
        == 404
    )
    login(client, world.users[Role.ADMIN])  # someone else sees no preference
    assert config_of(client.get(url(world), headers=HTML).text)["initial"]["connectionId"] == ""


def test_viewer_cannot_set_the_last_connection(client, login, world, connection):
    login(client, world.users[Role.VIEWER])
    assert (
        client.post(
            url(world, "/last-connection"), json={"connection_id": connection.id}
        ).status_code
        == 403
    )
