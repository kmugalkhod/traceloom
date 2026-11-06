"""Organization invites: create, look up and accept (FR-A4)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from traceloom.auth.permissions import Perm, Role, can
from traceloom.config import Settings
from traceloom.db.models import Invite, Membership, Organization, User
from traceloom.services import audit
from traceloom.services.accounts import AccountError, hash_token, normalize_email
from traceloom.services.mail import Mailer
from traceloom.services.orgs import NotAllowed, OrgError


def find_valid(db: Session, token: str, now: datetime | None = None) -> Invite | None:
    """The invite for this token if it is neither used nor expired."""
    invite = db.scalar(select(Invite).where(Invite.token_hash == hash_token(token)))
    if (
        invite is None
        or invite.accepted_at is not None
        or invite.expires_at <= (now or datetime.now(UTC))
    ):
        return None
    return invite


def accept(db: Session, invite: Invite, user: User) -> Membership:
    """Join the invite's org with the invited role. A user who is already a member keeps the
    higher of the two roles."""
    membership = db.get(Membership, (user.id, invite.org_id))
    if membership is None:
        membership = Membership(user_id=user.id, org_id=invite.org_id, role=invite.role)
        db.add(membership)
    elif Role.parse(invite.role) > Role.parse(membership.role):
        membership.role = invite.role
    invite.accepted_at = datetime.now(UTC)
    db.flush()
    return membership


# --- creating and managing invites ---


def list_invites(db: Session, org_id: str) -> list[Invite]:
    return list(
        db.scalars(
            select(Invite)
            .where(Invite.org_id == org_id, Invite.accepted_at.is_(None))
            .order_by(Invite.expires_at)
        )
    )


def create_invite(
    db: Session,
    settings: Settings,
    mailer: Mailer,
    actor: User,
    actor_role: Role,
    org: Organization,
    email: str,
    role: Role,
) -> Invite:
    """Invite an email to the org with a role. Re-inviting replaces the earlier pending invite."""
    if not can(actor_role, Perm.INVITES):
        raise NotAllowed("Only an Admin or Owner can invite people.")
    if role is Role.OWNER and not can(actor_role, Perm.OWNER_GRANT):
        raise NotAllowed("Only an Owner can invite someone as Owner.")
    try:
        address = normalize_email(email)
    except AccountError as exc:
        raise OrgError(str(exc)) from exc
    existing = db.scalar(select(User).where(User.email == address))
    if existing is not None and db.get(Membership, (existing.id, org.id)) is not None:
        raise OrgError(f"{address} is already a member of {org.name}.")
    db.execute(
        delete(Invite).where(
            Invite.org_id == org.id, Invite.email == address, Invite.accepted_at.is_(None)
        )
    )
    token = secrets.token_urlsafe(32)
    invite = Invite(
        org_id=org.id,
        email=address,
        role=role.label,
        token_hash=hash_token(token),
        expires_at=datetime.now(UTC) + timedelta(hours=settings.invite_ttl_hours),
    )
    db.add(invite)
    db.flush()
    audit.record(
        db,
        action="invite.created",
        actor_type="user",
        actor_id=actor.id,
        org_id=org.id,
        target=invite.id,
        detail={"email": address, "role": role.label},
    )
    mailer.send(
        address,
        f"{actor.name or actor.email} invited you to {org.name} on Traceloom",
        f"You were invited to join {org.name} as {role.label.title()}.\n\n"
        f"Accept the invite: {settings.base_url.rstrip('/')}/invite/{token}\n\n"
        f"The link works once and expires in {settings.invite_ttl_hours // 24 or 1} day(s).",
    )
    return invite


def revoke_invite(db: Session, actor: User, actor_role: Role, org_id: str, invite_id: str) -> None:
    if not can(actor_role, Perm.INVITES):
        raise NotAllowed("Only an Admin or Owner can revoke invites.")
    invite = db.get(Invite, invite_id)
    if invite is None or invite.org_id != org_id or invite.accepted_at is not None:
        raise OrgError("That invite no longer exists.")
    db.delete(invite)
    audit.record(
        db,
        action="invite.revoked",
        actor_type="user",
        actor_id=actor.id,
        org_id=org_id,
        target=invite_id,
        detail={"email": invite.email},
    )
    db.flush()
