"""LLM connections: encrypted secrets, SSRF-checked URLs, test-connection, roles, audit."""

from __future__ import annotations

import dataclasses
import json
import logging

import pytest
from sqlalchemy import select, text

from traceloom.auth.permissions import Role
from traceloom.db.models import AuditLog
from traceloom.llm import connections, providers
from traceloom.llm.connections import ConnectionInvalid, ConnectionPolicy
from traceloom.llm.crypto import SecretBox, generate_key
from traceloom.llm.providers import BaseProvider, DoneDelta, TextDelta
from traceloom.prompts.service import Actor, NotAllowed

KEY = "sk-ant-api03-supersecretkey9876"
PUBLIC = "93.184.216.34"


@pytest.fixture
def policy(settings):
    return ConnectionPolicy.from_settings(settings, resolver=lambda host, port: [PUBLIC])


def actor(world, role):
    return Actor.for_user(world.users[role], role)


@pytest.fixture
def admin(world):
    return actor(world, Role.ADMIN)


@pytest.fixture
def member(world):
    return actor(world, Role.MEMBER)


@pytest.fixture
def conn(db, world, admin, policy):
    row = connections.create(
        db,
        world.project,
        admin,
        policy,
        provider="anthropic",
        name="Claude",
        secrets={"api_key": KEY},
        custom_models=["my-finetune"],
    )
    db.commit()
    return row


def dump_db(db) -> str:
    """Every row of every table as text, to prove a value is nowhere in storage."""
    out = []
    for (table,) in db.execute(text("select name from sqlite_master where type='table'")).all():
        out.append(json.dumps(db.execute(text(f'select * from "{table}"')).all(), default=str))
    return "\n".join(out)


class Echo(BaseProvider):
    """A provider whose failure message repeats what it was given, as real APIs sometimes do."""

    def __init__(self, cfg):
        super().__init__(cfg)

    def _stream(self, *args):
        raise RuntimeError(f"401 invalid key {self.cfg.api_key}")
        yield


@pytest.fixture(autouse=True)
def restore_registry():
    yield
    for name in providers.PROVIDERS:
        providers.unregister(name)


def test_secret_is_encrypted_and_only_a_hint_is_exposed(db, world, admin, policy, conn, settings):
    assert conn.secret_hint == "…9876"
    assert KEY not in dump_db(db)
    stored = SecretBox(settings.encryption_key).decrypt(conn.secret_ciphertext)
    assert json.loads(stored)["api_key"] == KEY
    view = connections.get_view(db, world.project, admin, policy, conn.id)
    assert KEY not in repr(view) and view.secret_hint == "…9876"
    assert view.models[-1] == "my-finetune" and "claude-sonnet-5-5" in view.models
    assert not hasattr(view, "secret_ciphertext")


def test_update_keeps_or_replaces_the_secret(db, world, admin, policy, conn):
    connections.update(db, world.project, admin, policy, conn.id, name="Renamed")
    cfg = connections.provider_config(policy, conn)
    assert cfg.api_key == KEY and conn.name == "Renamed"
    connections.update(
        db, world.project, admin, policy, conn.id, secrets={"api_key": "sk-ant-new-value-4321"}
    )
    assert connections.provider_config(policy, conn).api_key == "sk-ant-new-value-4321"
    assert conn.secret_hint == "…4321"
    assert "supersecret" not in dump_db(db)


def test_headers_are_encrypted_and_only_names_shown(db, world, admin, policy):
    row = connections.create(
        db, world.project, admin, policy, provider="openai_compatible", name="gw",
        base_url="https://gw.example.test/v1", headers={"X-Tenant": "tenant-secret-777"},
    )  # fmt: skip
    db.commit()
    assert "tenant-secret-777" not in dump_db(db)
    view = connections.get_view(db, world.project, admin, policy, row.id)
    assert view.header_names == ["X-Tenant"] and "tenant-secret" not in repr(view)
    assert connections.provider_config(policy, row).config["headers"] == {
        "X-Tenant": "tenant-secret-777"
    }


def test_failing_test_connection_never_reveals_the_secret(db, world, admin, policy, conn, caplog):
    providers.register("anthropic", Echo)
    caplog.set_level(logging.DEBUG)
    result = connections.test(db, world.project, admin, policy, conn.id)
    assert not result.ok and "401 invalid key" in result.message
    assert KEY not in result.message and KEY not in caplog.text
    assert KEY not in dump_db(db)


def test_failure_raised_while_building_the_adapter_is_scrubbed(db, world, admin, policy, conn):
    def broken(cfg):
        raise RuntimeError(f"cannot init with {cfg.api_key}")

    providers.register("anthropic", broken)
    result = connections.test(db, world.project, admin, policy, conn.id)
    assert not result.ok and KEY not in result.message


def test_successful_test_connection(db, world, admin, policy, conn, fake_provider):
    providers.register("anthropic", fake_provider)
    fake_provider.script(TextDelta("ok"), DoneDelta("stop"))
    result = connections.test(db, world.project, admin, policy, conn.id, model="claude-sonnet-5-5")
    assert result.ok and result.model == "claude-sonnet-5-5"
    call = fake_provider.calls[0]
    assert call["model"] == "claude-sonnet-5-5" and call["params"] == {"max_tokens": 16}
    assert fake_provider.config.api_key == KEY  # the adapter got the decrypted key


def test_undecryptable_key_asks_for_reentry_instead_of_failing(
    db, world, admin, member, conn, settings
):
    rotated = ConnectionPolicy(SecretBox(generate_key()), True, 30.0, lambda h, p: [PUBLIC])
    view = connections.get_view(db, world.project, admin, rotated, conn.id)
    assert view.needs_reentry and view.status == "key needs re-entry"
    assert connections.list_connections(db, world.project, admin, rotated)[0].needs_reentry
    assert (
        connections.test(db, world.project, admin, rotated, conn.id).message == "key needs re-entry"
    )
    assert connections.list_usable(db, world.project, member, rotated) == []
    with pytest.raises(ConnectionInvalid, match="re-entry"):
        connections.open_provider(db, world.project, member, rotated, conn.id)
    # entering the key again repairs it
    connections.update(db, world.project, admin, rotated, conn.id, secrets={"api_key": KEY})
    assert not connections.get_view(db, world.project, admin, rotated, conn.id).needs_reentry


def test_members_can_list_usable_connections_but_not_edit(db, world, admin, member, policy, conn):
    usable = connections.list_usable(db, world.project, member, policy)
    assert [(u.name, u.provider) for u in usable] == [("Claude", "anthropic")]
    assert "my-finetune" in usable[0].models
    assert {f.name for f in dataclasses.fields(usable[0])} == {"id", "name", "provider", "models"}
    with pytest.raises(NotAllowed):
        connections.list_connections(db, world.project, member, policy)
    with pytest.raises(NotAllowed):
        connections.get_view(db, world.project, member, policy, conn.id)
    with pytest.raises(NotAllowed):
        connections.create(
            db, world.project, member, policy, provider="openai", name="x", secrets={"api_key": "k"}
        )
    with pytest.raises(NotAllowed):
        connections.update(db, world.project, member, policy, conn.id, name="x")
    with pytest.raises(NotAllowed):
        connections.delete(db, world.project, member, conn.id)
    with pytest.raises(NotAllowed):
        connections.test(db, world.project, member, policy, conn.id)
    viewer = actor(world, Role.VIEWER)
    with pytest.raises(NotAllowed):
        connections.list_usable(db, world.project, viewer, policy)


def test_create_update_delete_are_audited_without_secrets(db, world, admin, policy, conn):
    connections.update(db, world.project, admin, policy, conn.id, name="Renamed")
    connections.delete(db, world.project, admin, conn.id)
    db.commit()
    rows = db.scalars(
        select(AuditLog)
        .where(AuditLog.action.like("llm_connection.%"))
        .order_by(AuditLog.created_at)
    ).all()
    assert [r.action for r in rows] == [
        "llm_connection.created",
        "llm_connection.updated",
        "llm_connection.deleted",
    ]
    assert all(r.project_id == world.project.id and r.actor_id == admin.id for r in rows)
    assert KEY not in json.dumps([r.detail for r in rows]) and "9876" not in json.dumps(
        [r.detail for r in rows]
    )
    assert connections.list_connections(db, world.project, admin, policy) == []


@pytest.mark.parametrize("url", ["http://169.254.169.254/v1", "https://metadata.test/v1"])
def test_metadata_base_url_is_always_refused(db, world, admin, settings, url):
    for allow in (True, False):
        policy = ConnectionPolicy(
            SecretBox(settings.encryption_key), allow, 30.0, lambda h, p: ["169.254.169.254"]
        )
        with pytest.raises(ConnectionInvalid, match="not allowed"):
            connections.create(
                db,
                world.project,
                admin,
                policy,
                provider="openai_compatible",
                name="x",
                base_url=url,
            )


def test_private_base_url_follows_the_policy(db, world, admin, settings):
    def policy(allow):
        return ConnectionPolicy(
            SecretBox(settings.encryption_key), allow, 30.0, lambda h, p: ["10.0.0.5"]
        )

    kwargs = dict(provider="openai_compatible", name="local", base_url="http://ollama.lan:11434/v1")
    connections.create(db, world.project, admin, policy(True), **kwargs)  # the default
    with pytest.raises(ConnectionInvalid):
        connections.create(db, world.project, admin, policy(False), **kwargs)


def test_the_url_is_checked_again_before_use(db, world, admin, member, settings):
    answers = [[PUBLIC], ["169.254.169.254"]]
    policy = ConnectionPolicy(
        SecretBox(settings.encryption_key), True, 30.0, lambda h, p: answers.pop(0)
    )
    row = connections.create(
        db, world.project, admin, policy, provider="openai_compatible", name="x",
        base_url="https://gw.example.test/v1",
    )  # fmt: skip
    with pytest.raises(ConnectionInvalid):
        connections.open_provider(db, world.project, member, policy, row.id)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(provider="anthropic", name="x"),  # no key
        dict(provider="nope", name="x", secrets={"api_key": "k"}),
        dict(provider="openai", name="  ", secrets={"api_key": "k"}),
        dict(provider="openai_compatible", name="x"),  # no base URL
        dict(provider="azure_openai", name="x", secrets={"api_key": "k"},
             base_url="https://r.openai.azure.test"),  # no api version
        dict(provider="bedrock", name="x", secrets={"api_key": "s", "access_key_id": "a"}),
    ],
)  # fmt: skip
def test_per_provider_credential_shapes_are_enforced(db, world, admin, policy, kwargs):
    with pytest.raises(ConnectionInvalid):
        connections.create(db, world.project, admin, policy, **kwargs)


def test_valid_shapes_for_azure_and_bedrock(db, world, admin, policy):
    azure = connections.create(
        db, world.project, admin, policy, provider="azure_openai", name="az",
        secrets={"api_key": "azure-key-1234"}, base_url="https://r.openai.azure.test",
        config={"api_version": "2025-04-01-preview", "deployments": {"gpt-4o": "prod"}, "junk": 1},
    )  # fmt: skip
    assert azure.config == {"api_version": "2025-04-01-preview", "deployments": {"gpt-4o": "prod"}}
    bedrock = connections.create(
        db, world.project, admin, policy, provider="bedrock", name="bk",
        secrets={"api_key": "aws-secret-5555", "access_key_id": "AKIAEXAMPLE0000"},
        config={"region": "us-west-2"},
    )  # fmt: skip
    cfg = connections.provider_config(policy, bedrock)
    assert (
        cfg.secrets == {"access_key_id": "AKIAEXAMPLE0000"} and cfg.config["region"] == "us-west-2"
    )
    assert "AKIAEXAMPLE0000" not in dump_db(db)


def test_connections_are_project_scoped(db, world, admin, policy, conn, make_project):
    other = make_project(world.org)
    with pytest.raises(connections.ConnectionNotFound):
        connections.get_view(db, other, admin, policy, conn.id)
    with pytest.raises(connections.ConnectionNotFound):
        connections.delete(db, other, admin, conn.id)
    assert connections.list_connections(db, other, admin, policy) == []
    assert connections.list_usable(db, other, actor(world, Role.MEMBER), policy) == []


def test_dns_rebinding_after_the_open_check_is_blocked_at_connect(
    db, world, admin, member, settings
):
    """open_provider checks the base URL; the adapter's HTTP client checks again for every
    request and connects to the address it checked, so a flip in between is caught."""
    answers = [PUBLIC, PUBLIC, "169.254.169.254"]
    flipping = ConnectionPolicy.from_settings(
        settings, resolver=lambda host, port: [answers.pop(0) if len(answers) > 1 else answers[0]]
    )
    row = connections.create(
        db, world.project, admin, flipping, provider="openai", name="o",
        secrets={"api_key": "sk-test-rebinding-0123"}, base_url="https://llm.example.test/v1",
    )  # fmt: skip
    adapter = connections.open_provider(db, world.project, member, flipping, row.id)
    out = list(adapter.stream([{"role": "user", "content": "hi"}], "gpt-4o"))
    assert len(out) == 1 and "resolves to a blocked address" in out[0].message, out
    assert answers == ["169.254.169.254"]  # the third lookup was the adapter's, at connect time
