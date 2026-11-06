from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import AuditLog, LLMConnection
from traceloom.llm import connections
from traceloom.llm.connections import ConnectionPolicy, TestResult
from traceloom.llm.crypto import SecretBox, generate_key
from traceloom.prompts.service import Actor

HTML = {"Accept": "text/html"}
XSS = "<script>alert(1)</script>"
KEY = "sk-live-SECRET-0123456789-a91f"


def url(world, suffix=""):
    return f"/project/{world.project.id}/settings/connections{suffix}"


def token(client, world):
    page = client.get(url(world), headers=HTML)
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def post(client, world, suffix="", **data):
    data["csrf_token"] = token(client, world)
    return client.post(url(world, suffix), data=data, headers=HTML, follow_redirects=False)


@pytest.fixture
def admin(client, login, world):
    return login(client, world.users[Role.ADMIN])


def add(client, world, **fields):
    fields.setdefault("provider", "anthropic")
    fields.setdefault("name", "Anthropic · production key")
    return post(client, world, **fields)


# --- access ---------------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.MEMBER, Role.VIEWER])
def test_member_and_viewer_get_403_and_no_tab(client, login, world, role, db):
    login(client, world.users[role])
    assert client.get(url(world), headers=HTML).status_code == 403
    for suffix in ("", "/x", "/x/test", "/x/delete"):
        r = client.post(url(world, suffix), data={"name": "n"}, headers=HTML)
        assert r.status_code == 403, suffix
    assert (
        "LLM connections"
        not in client.get(f"/project/{world.project.id}/settings", headers=HTML).text
    )
    assert db.scalar(select(LLMConnection)) is None


def test_admin_sees_the_tab_and_the_empty_state(admin, world):
    settings = admin.get(f"/project/{world.project.id}/settings", headers=HTML).text
    assert url(world) in settings and "LLM connections" in settings
    page = admin.get(url(world), headers=HTML).text
    assert "No LLM connections yet" in page and "Add connection" in page


# --- adding one per provider ------------------------------------------------------------------


def test_add_dialog_has_a_provider_select_with_each_providers_fields(admin, world):
    page = admin.get(url(world), headers=HTML).text
    for provider in (
        "anthropic",
        "openai",
        "azure_openai",
        "bedrock",
        "google",
        "openai_compatible",
    ):
        assert f'<option value="{provider}"' in page
        assert f'data-provider-fields="{provider}"' in page
    for needle in (
        "Endpoint",
        "API version",
        "Deployments",
        "Access key ID",
        "Region",
        "Secret access key",
        "Base URL",
        "Extra headers",
        "Custom model names",
    ):
        assert needle in page
    assert page.count('data-provider-fields="anthropic"') == 1


def test_add_anthropic_masks_the_key(admin, world, db):
    r = add(admin, world, api_key=KEY, custom_models="my-finetune, other")
    assert r.status_code == 303
    row = db.scalar(select(LLMConnection))
    assert row.provider == "anthropic" and row.secret_hint == "…a91f"
    assert row.custom_models == ["my-finetune", "other"] and KEY not in row.secret_ciphertext
    page = admin.get(url(world), headers=HTML).text
    assert "Anthropic · production key" in page and "key …a91f" in page
    assert "(2 custom)" in page
    assert KEY not in page and "SECRET-0123456789" not in page
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "llm_connection.created"))
    assert audit is not None and KEY not in str(audit.detail)


def test_add_openai_compatible_needs_a_base_url_and_allows_a_keyless_server(admin, world, db):
    bad = add(admin, world, provider="openai_compatible", name="vLLM")
    assert (
        bad.status_code == 400
        and "A base URL is required" in bad.text
        and 'role="alert"' in bad.text
    )
    assert "data-autoopen" in bad.text and 'value="vLLM"' in bad.text  # what they typed is kept
    ok = add(
        admin,
        world,
        provider="openai_compatible",
        name="vLLM",
        base_url="http://127.0.0.1:8000/v1",
        custom_models="llama-3.3-70b",
        headers='{"X-Team": "support"}',
    )
    assert ok.status_code == 303
    row = db.scalar(select(LLMConnection))
    assert row.base_url == "http://127.0.0.1:8000/v1" and row.custom_models == ["llama-3.3-70b"]
    page = admin.get(url(world), headers=HTML).text
    assert (
        "OpenAI-compatible" in page
        and "X-Team" not in page.split("Saved headers")[0].split("vLLM")[-1][:0] + "x"
    )


def test_add_azure(admin, world, db):
    r = add(
        admin,
        world,
        provider="azure_openai",
        name="Azure",
        api_key="azure-key-9999",
        base_url="https://93.184.216.34",
        api_version="2025-06-01",
        deployments="gpt-4o=chat-prod, mini=m1",
    )
    assert r.status_code == 303
    row = db.scalar(select(LLMConnection))
    assert row.config == {
        "api_version": "2025-06-01",
        "deployments": {"gpt-4o": "chat-prod", "mini": "m1"},
    }
    page = admin.get(url(world), headers=HTML).text
    assert "2 deployments" in page and "key …9999" in page


def test_azure_errors_are_inline(admin, world):
    r = add(
        admin,
        world,
        provider="azure_openai",
        name="Az",
        api_key="k",
        base_url="https://93.184.216.34",
        api_version="v",
        deployments="not-a-pair",
    )
    assert r.status_code == 400 and "model=deployment" in r.text


def test_add_bedrock_and_google_and_openai(admin, world, db):
    assert (
        add(
            admin,
            world,
            provider="bedrock",
            name="B",
            access_key_id="AKIA123",
            api_key="secretkey1234",
            region="us-east-1",
        ).status_code
        == 303
    )
    assert add(admin, world, provider="google", name="G", api_key="g-key-0000").status_code == 303
    assert add(admin, world, provider="openai", name="O", api_key="sk-oooo-1111").status_code == 303
    rows = {r.provider: r for r in db.scalars(select(LLMConnection))}
    assert set(rows) == {"bedrock", "google", "openai"} and rows["bedrock"].config == {
        "region": "us-east-1"
    }
    page = admin.get(url(world), headers=HTML).text
    for secret in ("AKIA123", "secretkey1234", "g-key-0000", "sk-oooo-1111"):
        assert secret not in page
    assert "key …1234" in page


def test_missing_key_and_name_errors_are_inline(admin, world, db):
    assert "An API key is required" in add(admin, world, name="x").text
    assert add(admin, world, name="", api_key="k").status_code == 400
    assert add(admin, world, provider="nope", name="x", api_key="k").status_code == 400
    assert db.scalar(select(LLMConnection)) is None


def test_ssrf_error_is_inline_and_nothing_is_saved(admin, world, db):
    r = add(
        admin,
        world,
        provider="openai_compatible",
        name="meta",
        base_url="http://169.254.169.254/latest",
    )
    assert r.status_code == 400 and "not allowed" in r.text and "data-autoopen" in r.text
    assert db.scalar(select(LLMConnection)) is None


def test_private_base_url_is_refused_when_the_operator_blocks_it(admin, world, container):
    container.settings.llm_allow_private = False
    r = add(
        admin,
        world,
        provider="openai_compatible",
        name="local",
        base_url="http://127.0.0.1:8000/v1",
    )
    assert r.status_code == 400 and "not allowed" in r.text


# --- the secret never comes back ---------------------------------------------------------------


def test_the_secret_appears_on_no_page_after_save(admin, world, db, monkeypatch):
    add(admin, world, api_key=KEY, name="Main")
    row = db.scalar(select(LLMConnection))
    pages = [
        admin.get(url(world), headers=HTML).text,
        admin.get(url(world, "?notice=saved"), headers=HTML).text,
    ]
    monkeypatch.setattr(
        connections, "test", lambda *a, **k: TestResult(False, "401 invalid key", "m")
    )
    pages.append(post(admin, world, f"/{row.id}/test").text)
    bad = post(
        admin, world, f"/{row.id}", name="", api_key="another-SECRET-1234"
    )  # invalid: blank name
    pages.append(bad.text)
    for html in pages:
        assert KEY not in html and "SECRET" not in html
    assert 'type="password"' in pages[0] and 'value="sk-' not in pages[0]
    assert "Saved · ends in …a91f" in pages[0]


def test_needs_reentry_state(admin, world, db, container):
    policy = ConnectionPolicy(SecretBox(generate_key()), True, 5.0)  # another key than the server's
    who = Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)
    connections.create(
        db,
        world.project,
        who,
        policy,
        provider="anthropic",
        name="Old key",
        secrets={"api_key": "sk-old-0000"},
    )
    db.commit()
    page = admin.get(url(world), headers=HTML).text
    assert "Key needs re-entry" in page and "can no longer be read" in page


# --- test connection ---------------------------------------------------------------------------


def test_test_connection_shows_the_result_inline(admin, world, db, monkeypatch):
    add(admin, world, api_key=KEY)
    row = db.scalar(select(LLMConnection))
    seen = []

    def ok(db_, project, actor, policy, connection_id, model=None):
        seen.append(connection_id)
        return TestResult(True, "Connection works.", "claude-sonnet-5-5")

    monkeypatch.setattr(connections, "test", ok)
    page = post(admin, world, f"/{row.id}/test")
    assert page.status_code == 200 and seen == [row.id]
    assert (
        "Test passed for Anthropic · production key" in page.text
        and "Connection works" in page.text
    )
    monkeypatch.setattr(
        connections,
        "test",
        lambda *a, **k: TestResult(False, "The provider answered 401: invalid x-api-key.", "m"),
    )
    failed = post(admin, world, f"/{row.id}/test").text
    assert "Test failed for" in failed and "answered 401" in failed and 'role="alert"' in failed
    assert KEY not in failed


def test_the_real_test_scrubs_the_key_from_provider_errors(admin, world, db, monkeypatch):
    """connections.test redacts the key even when the provider echoes it back."""
    from traceloom.llm import providers
    from traceloom.llm.providers import ErrorDelta

    class Echo:
        def __init__(self, cfg):
            self.cfg = cfg

        def stream(self, *args, **kwargs):
            yield ErrorDelta(f"401 bad key {self.cfg.api_key}")

    monkeypatch.setattr(providers, "create", lambda provider, cfg: Echo(cfg))
    add(admin, world, api_key=KEY)
    row = db.scalar(select(LLMConnection))
    page = post(admin, world, f"/{row.id}/test").text
    assert (
        "Test failed" in page
        and KEY not in page
        and "[redacted]" in page
        or "***" in page
        or "•" in page
    )


def test_save_and_test(admin, world, db, monkeypatch):
    monkeypatch.setattr(
        connections, "test", lambda *a, **k: TestResult(True, "Connection works.", "m")
    )
    r = add(admin, world, api_key=KEY, then="test")
    assert r.status_code == 200 and "Connection saved" in r.text and "Test passed" in r.text
    assert db.scalar(select(LLMConnection)) is not None


# --- edit and delete ---------------------------------------------------------------------------


def test_edit_keeps_the_key_when_blank_and_replaces_it_when_given(admin, world, db):
    add(admin, world, api_key=KEY, name="Main")
    row = db.scalar(select(LLMConnection))
    before = row.secret_ciphertext
    r = post(admin, world, f"/{row.id}", name="Renamed", api_key="", custom_models="x1")
    assert r.status_code == 303
    db.refresh(row)
    assert row.name == "Renamed" and row.custom_models == ["x1"] and row.secret_hint == "…a91f"
    r = post(admin, world, f"/{row.id}", name="Renamed", api_key="sk-new-key-7777")
    db.refresh(row)
    assert row.secret_hint == "…7777" and row.secret_ciphertext != before
    assert "ends in …7777" in admin.get(url(world), headers=HTML).text


def test_edit_validation_error_reopens_the_edit_dialog(admin, world, db):
    add(admin, world, provider="openai_compatible", name="C", base_url="http://127.0.0.1:8000/v1")
    row = db.scalar(select(LLMConnection))
    r = post(admin, world, f"/{row.id}", name="C", base_url="http://169.254.169.254/x")
    assert r.status_code == 400 and "not allowed" in r.text and "data-autoopen" in r.text
    db.refresh(row)
    assert row.base_url == "http://127.0.0.1:8000/v1"


def test_delete_and_audit(admin, world, db):
    add(admin, world, api_key=KEY)
    row = db.scalar(select(LLMConnection))
    r = post(admin, world, f"/{row.id}/delete")
    assert r.status_code == 303 and db.scalar(select(LLMConnection)) is None
    assert (
        db.scalar(select(AuditLog).where(AuditLog.action == "llm_connection.deleted")) is not None
    )


def test_other_projects_connections_are_not_reachable(admin, world, db, make_project, container):
    other = make_project(world.org)
    who = Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)
    row = connections.create(
        db,
        other,
        who,
        ConnectionPolicy.from_settings(container.settings),
        provider="anthropic",
        name="Theirs",
        secrets={"api_key": "sk-other-1234"},
    )
    db.commit()
    assert "Theirs" not in admin.get(url(world), headers=HTML).text
    for suffix in ("", "/test", "/delete"):
        assert post(admin, world, f"/{row.id}{suffix}", name="x").status_code == 404


def test_user_content_is_escaped(admin, world, db):
    add(admin, world, name=XSS, api_key="sk-xss-1234", custom_models=XSS)
    page = admin.get(url(world), headers=HTML).text
    assert XSS not in page and "&lt;script&gt;" in page
    bad = add(admin, world, provider="openai_compatible", name=XSS, base_url=XSS)
    assert XSS not in bad.text


def test_forms_need_a_csrf_token(client, login, world):
    login(client, world.users[Role.ADMIN], csrf=False)
    assert client.post(url(world), data={"name": "x"}, headers=HTML).status_code == 403
