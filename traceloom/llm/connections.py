"""Project LLM connections: encrypted credentials, SSRF-checked base URLs, test-connection.

All secret material (API key, Bedrock access key id / session token, custom header values) lives in
one Fernet-encrypted blob. It is replaced when new values are supplied and never read back: the UI
gets a `…last4` hint, the header names and a `needs_reentry` flag. Admins manage connections;
Members only see the names and models of the usable ones. Functions add to the caller's
transaction (the caller commits).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceloom.auth.permissions import Perm
from traceloom.config import Settings
from traceloom.db.base import new_id, utcnow
from traceloom.db.models import LLMConnection, Project
from traceloom.llm import providers
from traceloom.llm.crypto import CryptoError, SecretBox, hint
from traceloom.llm.models import models_for
from traceloom.llm.providers import ErrorDelta, ProviderConfig, scrub
from traceloom.llm.providers.bedrock import check_endpoint as check_bedrock_endpoint
from traceloom.llm.ssrf import BlockedAddress, Resolver, resolve_target, system_resolver
from traceloom.prompts.service import Actor
from traceloom.services import audit

log = logging.getLogger("traceloom.llm")

NEEDS_REENTRY = "key needs re-entry"
SECRET_FIELDS = ("api_key", "access_key_id", "session_token")
MAX_NAME = 200
MAX_MODELS = 100


class ConnectionInvalid(ValueError):
    """The request is invalid; the message is safe to show."""


class ConnectionNotFound(LookupError):
    pass


@dataclass(frozen=True)
class ConnectionPolicy:
    box: SecretBox
    allow_private: bool
    timeout: float
    resolver: Resolver = system_resolver

    @classmethod
    def from_settings(cls, settings: Settings, resolver: Resolver = system_resolver):
        return cls(
            SecretBox(settings.encryption_key),
            settings.llm_allow_private,
            float(settings.provider_timeout_seconds),
            resolver,
        )


@dataclass(frozen=True)
class ConnectionView:
    """What the settings page may show about a connection (no secrets)."""

    id: str
    provider: str
    name: str
    base_url: str | None
    config: dict[str, Any]
    secret_hint: str
    header_names: list[str]
    custom_models: list[str]
    models: list[str]
    needs_reentry: bool
    created_at: datetime
    updated_at: datetime

    @property
    def status(self) -> str:
        return NEEDS_REENTRY if self.needs_reentry else "ok"


@dataclass(frozen=True)
class UsableConnection:
    """What a Member sees when picking a connection in the playground."""

    id: str
    name: str
    provider: str
    models: list[str]


@dataclass(frozen=True)
class TestResult:
    ok: bool
    message: str
    model: str | None = None
    __test__ = False  # not a pytest class


@dataclass
class Secrets:
    api_key: str = ""
    access_key_id: str = ""
    session_token: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    def dump(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def load(cls, raw: str) -> Secrets:
        data = json.loads(raw)
        return cls(
            **{k: data.get(k, "") for k in SECRET_FIELDS}, headers=dict(data.get("headers") or {})
        )

    def values(self) -> list[str]:
        return [v for v in (self.api_key, self.access_key_id, self.session_token) if v] + [
            v for v in self.headers.values() if v
        ]


# --- validation -----------------------------------------------------------------------------


def _clean_models(models: Sequence[str] | None) -> list[str]:
    cleaned = list(dict.fromkeys(m.strip() for m in (models or ()) if m and m.strip()))
    if len(cleaned) > MAX_MODELS or any(len(m) > 200 for m in cleaned):
        raise ConnectionInvalid("Too many custom models, or a model name is too long.")
    return cleaned


def _check_base_url(policy: ConnectionPolicy, url: str | None) -> str | None:
    url = (url or "").strip() or None
    if url is None:
        return None
    try:
        resolve_target(url, allow_private=policy.allow_private, resolver=policy.resolver)
    except BlockedAddress as exc:
        raise ConnectionInvalid(f"This base URL is not allowed: {exc}") from None
    return url


def _validate_shape(provider: str, base_url: str | None, config: dict, secrets: Secrets) -> None:
    def need(ok: bool, message: str) -> None:
        if not ok:
            raise ConnectionInvalid(message)

    if provider in ("anthropic", "openai", "google"):
        need(bool(secrets.api_key), "An API key is required.")
    elif provider == "openai_compatible":
        need(bool(base_url), "A base URL is required.")
    elif provider == "azure_openai":
        need(bool(secrets.api_key), "An API key is required.")
        need(bool(base_url), "The Azure endpoint is required.")
        need(bool(config.get("api_version")), "The API version is required.")
        deployments = config.get("deployments") or {}
        need(
            isinstance(deployments, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in deployments.items()),
            "Deployments must map model names to deployment names.",
        )
    elif provider == "bedrock":
        need(
            bool(secrets.access_key_id and secrets.api_key), "An access key and secret are needed."
        )
        need(bool(config.get("region")), "The region is required.")
        if base_url:
            try:
                check_bedrock_endpoint(base_url)
            except BlockedAddress as exc:
                raise ConnectionInvalid(f"This endpoint is not allowed: {exc}.") from None


def _public_config(provider: str, config: Mapping[str, Any] | None) -> dict[str, Any]:
    keep = {
        "azure_openai": ("api_version", "deployments"),
        "bedrock": ("region",),
    }.get(provider, ())
    return {k: config[k] for k in keep if config and config.get(k)}


def _merge(base: Secrets, new: Mapping[str, str] | None, headers: Mapping[str, str] | None):
    merged = Secrets(base.api_key, base.access_key_id, base.session_token, dict(base.headers))
    for key in SECRET_FIELDS:
        if new and new.get(key):
            setattr(merged, key, new[key].strip())
    if headers is not None:
        merged.headers = {k.strip(): str(v) for k, v in headers.items() if k and k.strip()}
    return merged


def _decrypt(policy: ConnectionPolicy, row: LLMConnection) -> Secrets | None:
    """The stored secrets, or None when the ciphertext is unreadable (wrong or rotated key)."""
    try:
        return Secrets.load(policy.box.decrypt(row.secret_ciphertext))
    except (CryptoError, ValueError, TypeError):
        return None


# --- access ---------------------------------------------------------------------------------


def _require_manage(actor: Actor) -> None:
    actor.require(Perm.LLM_CONNECTIONS, "Only admins can manage LLM connections.")


def get(db: Session, project: Project, connection_id: str) -> LLMConnection:
    row = db.scalar(
        select(LLMConnection).where(
            LLMConnection.id == connection_id, LLMConnection.project_id == project.id
        )
    )
    if row is None:
        raise ConnectionNotFound(connection_id)
    return row


def _view(policy: ConnectionPolicy, row: LLMConnection) -> ConnectionView:
    secrets = _decrypt(policy, row)
    return ConnectionView(
        id=row.id,
        provider=row.provider,
        name=row.name,
        base_url=row.base_url,
        config=dict(row.config or {}),
        secret_hint=row.secret_hint,
        header_names=sorted(secrets.headers) if secrets else [],
        custom_models=list(row.custom_models or []),
        models=models_for(row.provider, row.custom_models or []),
        needs_reentry=secrets is None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def list_connections(
    db: Session, project: Project, actor: Actor, policy: ConnectionPolicy
) -> list[ConnectionView]:
    _require_manage(actor)
    rows = db.scalars(
        select(LLMConnection)
        .where(LLMConnection.project_id == project.id)
        .order_by(LLMConnection.created_at, LLMConnection.id)
    )
    return [_view(policy, r) for r in rows]


def get_view(
    db: Session, project: Project, actor: Actor, policy: ConnectionPolicy, connection_id: str
) -> ConnectionView:
    _require_manage(actor)
    return _view(policy, get(db, project, connection_id))


def list_usable(
    db: Session, project: Project, actor: Actor, policy: ConnectionPolicy
) -> list[UsableConnection]:
    """Connections a Member may pick in the playground: names and models, never settings.
    Connections whose key cannot be read are left out."""
    actor.require(Perm.PLAYGROUND_RUN, "You need the Member role to use the playground.")
    rows = db.scalars(
        select(LLMConnection)
        .where(LLMConnection.project_id == project.id)
        .order_by(LLMConnection.name, LLMConnection.id)
    )
    return [
        UsableConnection(r.id, r.name, r.provider, models_for(r.provider, r.custom_models or []))
        for r in rows
        if _decrypt(policy, r) is not None
    ]


# --- writes ---------------------------------------------------------------------------------


def _audit(db: Session, project: Project, actor: Actor, action: str, row: LLMConnection) -> None:
    audit.record(
        db,
        action=action,
        actor_type=actor.type,
        actor_id=actor.id,
        org_id=project.org_id,
        project_id=project.id,
        target=row.id,
        detail={"provider": row.provider, "name": row.name},  # never the secret or its hint
    )


def _name(value: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > MAX_NAME:
        raise ConnectionInvalid("Enter a name of up to 200 characters.")
    return value


def create(
    db: Session,
    project: Project,
    actor: Actor,
    policy: ConnectionPolicy,
    *,
    provider: str,
    name: str,
    secrets: Mapping[str, str] | None = None,
    base_url: str | None = None,
    config: Mapping[str, Any] | None = None,
    custom_models: Sequence[str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> LLMConnection:
    _require_manage(actor)
    if provider not in providers.PROVIDERS:
        raise ConnectionInvalid("Unknown provider.")
    stored = _merge(Secrets(), secrets, headers)
    url = _check_base_url(policy, base_url)
    public = _public_config(provider, config)
    _validate_shape(provider, url, public, stored)
    now = utcnow()
    row = LLMConnection(
        id=new_id(),
        project_id=project.id,
        provider=provider,
        name=_name(name),
        base_url=url,
        config=public,
        secret_ciphertext=policy.box.encrypt(stored.dump()),
        secret_hint=hint(stored.api_key) if stored.api_key else "",
        custom_models=_clean_models(custom_models),
        created_by=actor.id,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    _audit(db, project, actor, "llm_connection.created", row)
    return row


def update(
    db: Session,
    project: Project,
    actor: Actor,
    policy: ConnectionPolicy,
    connection_id: str,
    *,
    name: str | None = None,
    secrets: Mapping[str, str] | None = None,
    base_url: str | None = None,
    config: Mapping[str, Any] | None = None,
    custom_models: Sequence[str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> LLMConnection:
    """Change a connection. Secrets left empty keep their stored value; supplied ones replace it
    (all of them must be supplied again when the stored ones cannot be decrypted)."""
    _require_manage(actor)
    row = get(db, project, connection_id)
    current = _decrypt(policy, row) or Secrets()
    stored = _merge(current, secrets, headers)
    url = _check_base_url(policy, base_url) if base_url is not None else row.base_url
    public = _public_config(row.provider, config) if config is not None else dict(row.config or {})
    _validate_shape(row.provider, url, public, stored)
    if name is not None:
        row.name = _name(name)
    if custom_models is not None:
        row.custom_models = _clean_models(custom_models)
    row.base_url, row.config = url, public
    row.secret_ciphertext = policy.box.encrypt(stored.dump())
    row.secret_hint = hint(stored.api_key) if stored.api_key else ""
    row.updated_at = utcnow()
    db.flush()
    _audit(db, project, actor, "llm_connection.updated", row)
    return row


def delete(db: Session, project: Project, actor: Actor, connection_id: str) -> None:
    _require_manage(actor)
    row = get(db, project, connection_id)
    _audit(db, project, actor, "llm_connection.deleted", row)
    db.delete(row)
    db.flush()


# --- using a connection ---------------------------------------------------------------------


def provider_config(policy: ConnectionPolicy, row: LLMConnection) -> ProviderConfig | None:
    """The decrypted adapter config, or None when the key needs re-entry."""
    secrets = _decrypt(policy, row)
    if secrets is None:
        return None
    config = dict(row.config or {})
    if secrets.headers:
        config["headers"] = dict(secrets.headers)
    extra = {
        k: getattr(secrets, k) for k in ("access_key_id", "session_token") if getattr(secrets, k)
    }
    return ProviderConfig(
        api_key=secrets.api_key,
        base_url=row.base_url,
        config=config,
        secrets=extra,
        timeout=policy.timeout,
        allow_private=policy.allow_private,
        resolver=policy.resolver,  # the adapters' HTTP clients check every request with it
    )


def open_provider(
    db: Session, project: Project, actor: Actor, policy: ConnectionPolicy, connection_id: str
):
    """A ready adapter for the playground (Members+). Raises ConnectionInvalid on a bad key."""
    actor.require(Perm.PLAYGROUND_RUN, "You need the Member role to use the playground.")
    row = get(db, project, connection_id)
    cfg = provider_config(policy, row)
    if cfg is None:
        raise ConnectionInvalid(NEEDS_REENTRY.capitalize() + ".")
    _check_base_url(policy, row.base_url)  # DNS may have changed since the connection was saved
    return providers.create(row.provider, cfg)


def test(
    db: Session,
    project: Project,
    actor: Actor,
    policy: ConnectionPolicy,
    connection_id: str,
    model: str | None = None,
) -> TestResult:
    """Make one minimal call and report success or the provider's (scrubbed) error."""
    _require_manage(actor)
    row = get(db, project, connection_id)
    cfg = provider_config(policy, row)
    if cfg is None:
        return TestResult(False, NEEDS_REENTRY)
    secrets = _decrypt(policy, row)
    known = secrets.values() if secrets else []
    model = model or (models_for(row.provider, row.custom_models or []) or [None])[0]
    try:
        _check_base_url(policy, row.base_url)
        adapter = providers.create(row.provider, cfg)
        for delta in adapter.stream(
            [{"role": "user", "content": "Reply with the single word: ok"}],
            model or "",
            {"max_tokens": 16},
        ):
            if isinstance(delta, ErrorDelta):
                return TestResult(False, scrub(delta.message, known), model)
    except Exception as exc:  # adapters report errors as deltas, but construction can raise
        log.warning("connection test failed for %s (%s)", row.id, type(exc).__name__)
        return TestResult(False, scrub(f"{type(exc).__name__}: {exc}", known), model)
    return TestResult(True, "Connection works.", model)
