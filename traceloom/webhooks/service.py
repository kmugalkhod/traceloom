"""Configure webhooks: create, update, delete and send-test (Admin+, audited).

The secret is generated here, returned once and stored Fernet-encrypted. A URL must be https
(http is accepted outside production) and is resolved and checked against the SSRF guard at save
time; delivery checks it again. Functions add to the caller's transaction; the caller commits.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceloom.auth.permissions import Perm
from traceloom.config import Settings
from traceloom.db.base import new_id, utcnow
from traceloom.db.models import Project, Webhook, WebhookDelivery
from traceloom.llm.crypto import SecretBox
from traceloom.llm.ssrf import BlockedAddress, Resolver, resolve_target, system_resolver
from traceloom.prompts.service import Actor
from traceloom.services import audit
from traceloom.webhooks import events

TEST_EVENT = "webhook.test"
MAX_URL = 2000


class WebhookError(ValueError):
    """The request is invalid; the message is safe to show."""


class WebhookNotFound(LookupError):
    pass


@dataclass(frozen=True)
class WebhookPolicy:
    box: SecretBox
    allow_private: bool
    allow_http: bool
    resolver: Resolver = system_resolver

    @classmethod
    def from_settings(cls, settings: Settings, resolver: Resolver = system_resolver):
        return cls(
            SecretBox(settings.encryption_key),
            settings.webhook_allow_private,
            settings.env != "production",
            resolver,
        )


def check_url(policy: WebhookPolicy, url: str) -> str:
    """The URL if it is acceptable, else WebhookError. Resolves the host (SSRF guard)."""
    url = (url or "").strip()
    if not url or len(url) > MAX_URL:
        raise WebhookError("Enter a URL of up to 2000 characters.")
    scheme = urlsplit(url).scheme
    if scheme != "https" and not (scheme == "http" and policy.allow_http):
        raise WebhookError("The URL must start with https://.")
    try:
        resolve_target(url, allow_private=policy.allow_private, resolver=policy.resolver)
    except BlockedAddress as exc:
        raise WebhookError(f"This URL is not allowed: {exc}") from None
    return url


def _events(selected: Sequence[str]) -> list[str]:
    chosen = list(dict.fromkeys(selected))
    unknown = [e for e in chosen if e not in events.EVENTS]
    if unknown or not chosen:
        raise WebhookError("Pick at least one known event.")
    return chosen


def _audit(db: Session, project: Project, actor: Actor, action: str, hook: Webhook) -> None:
    audit.record(
        db,
        action=action,
        actor_type=actor.type,
        actor_id=actor.id,
        org_id=project.org_id,
        project_id=project.id,
        target=hook.id,
        detail={"url": hook.url, "events": hook.events, "enabled": hook.enabled},  # no secret
    )


def _require(actor: Actor) -> None:
    actor.require(Perm.WEBHOOKS, "Only admins can manage webhooks.")


def get(db: Session, project: Project, webhook_id: str) -> Webhook:
    hook = db.scalar(
        select(Webhook).where(Webhook.id == webhook_id, Webhook.project_id == project.id)
    )
    if hook is None:
        raise WebhookNotFound(webhook_id)
    return hook


def list_webhooks(db: Session, project: Project, actor: Actor) -> list[Webhook]:
    _require(actor)
    stmt = select(Webhook).where(Webhook.project_id == project.id).order_by(Webhook.created_at)
    return list(db.scalars(stmt))


def list_deliveries(
    db: Session, project: Project, actor: Actor, webhook_id: str, limit: int = 50
) -> list[WebhookDelivery]:
    _require(actor)
    get(db, project, webhook_id)
    stmt = (
        select(WebhookDelivery)
        .where(WebhookDelivery.project_id == project.id, WebhookDelivery.webhook_id == webhook_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    return list(db.scalars(stmt))


def create(
    db: Session,
    project: Project,
    actor: Actor,
    policy: WebhookPolicy,
    *,
    url: str,
    event_names: Sequence[str],
    enabled: bool = True,
) -> tuple[Webhook, str]:
    """Returns the webhook and its plaintext secret, which is not retrievable afterwards."""
    _require(actor)
    secret = "whsec_" + secrets.token_urlsafe(32)
    hook = Webhook(
        id=new_id(),
        project_id=project.id,
        url=check_url(policy, url),
        secret_ciphertext=policy.box.encrypt(secret),
        events=_events(event_names),
        enabled=enabled,
        created_by=actor.id,
        created_at=utcnow(),
    )
    db.add(hook)
    db.flush()
    _audit(db, project, actor, "webhook.created", hook)
    return hook, secret


def update(
    db: Session,
    project: Project,
    actor: Actor,
    policy: WebhookPolicy,
    webhook_id: str,
    *,
    url: str | None = None,
    event_names: Sequence[str] | None = None,
    enabled: bool | None = None,
) -> Webhook:
    _require(actor)
    hook = get(db, project, webhook_id)
    if url is not None:
        hook.url = check_url(policy, url)
    if event_names is not None:
        hook.events = _events(event_names)
    if enabled is not None:
        hook.enabled = enabled
    db.flush()
    _audit(db, project, actor, "webhook.updated", hook)
    return hook


def delete(db: Session, project: Project, actor: Actor, webhook_id: str) -> None:
    _require(actor)
    hook = get(db, project, webhook_id)
    _audit(db, project, actor, "webhook.deleted", hook)
    db.delete(hook)
    db.flush()


def send_test(db: Session, project: Project, actor: Actor, webhook_id: str) -> WebhookDelivery:
    """Queue a `webhook.test` delivery for this webhook (even if disabled or not subscribed).

    The row is pending; call `deliver.kick(container)` after committing, or let the sweeper pick
    it up.
    """
    _require(actor)
    hook = get(db, project, webhook_id)
    now = utcnow()
    delivery = WebhookDelivery(
        id=new_id(),
        webhook_id=hook.id,
        project_id=project.id,
        event=TEST_EVENT,
        status="pending",
        attempts=0,
        next_attempt_at=now,
        created_at=now,
    )
    delivery.payload = {
        "id": delivery.id,
        "event": TEST_EVENT,
        "created_at": now.isoformat(),
        "project_id": project.id,
        "data": {"message": "Test delivery from Traceloom", "actor": actor.label},
    }
    db.add(delivery)
    db.flush()
    return delivery
