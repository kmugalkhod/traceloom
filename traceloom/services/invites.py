"""Organization invites: create, look up and accept (FR-A4)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceloom.db.models import Invite, Membership, User
from traceloom.services.accounts import hash_token


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
    from traceloom.auth.permissions import Role

    membership = db.get(Membership, (user.id, invite.org_id))
    if membership is None:
        membership = Membership(user_id=user.id, org_id=invite.org_id, role=invite.role)
        db.add(membership)
    elif Role.parse(invite.role) > Role.parse(membership.role):
        membership.role = invite.role
    invite.accepted_at = datetime.now(UTC)
    db.flush()
    return membership
