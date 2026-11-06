"""Demo data for `traceloom seed-demo`: a user, org, project and API key pair, idempotently.

The login comes from Settings (TRACELOOM_DEMO_EMAIL / TRACELOOM_DEMO_PASSWORD). The key pair is
derived from the server secret (TRACELOOM_SERVER_SECRET, read via Settings), so
running the command again prints the same keys (a stored secret cannot be recovered from its
hash) and they are unique to each install.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from traceloom.auth.api_keys import PUBLIC_PREFIX, SECRET_PREFIX, hash_secret
from traceloom.auth.passwords import hash_password
from traceloom.auth.permissions import Role
from traceloom.config import Settings
from traceloom.db.models import ApiKey, Membership, Organization, Project, User

DEMO_ORG = "Demo Org"
DEMO_PROJECT = "Demo Project"
DEMO_KEY_NOTE = "seed-demo"


@dataclass(frozen=True)
class DemoInfo:
    email: str
    password: str
    org_id: str
    project_id: str
    public_key: str
    secret_key: str
    created: bool


def _derive(settings: Settings, label: str, length: int) -> str:
    digest = hmac.new(settings.secret_key.encode(), f"seed-demo:{label}".encode(), hashlib.sha256)
    return digest.hexdigest()[:length]


def seed_demo(settings: Settings, session_factory: sessionmaker[Session]) -> DemoInfo:
    """Create the demo user, org, project and key if missing. Safe to run repeatedly."""
    email, password = settings.demo_email, settings.demo_password
    public_key = f"{PUBLIC_PREFIX}demo-{_derive(settings, 'public', 12)}"
    secret_key = f"{SECRET_PREFIX}demo-{_derive(settings, 'secret', 32)}"
    created = False
    with session_factory() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email, name="Demo User", password_hash=hash_password(password))
            db.add(user)
            created = True
        org = db.scalar(
            select(Organization)
            .join(Membership, Membership.org_id == Organization.id)
            .where(Membership.user_id == user.id, Organization.name == DEMO_ORG)
        )
        if org is None:
            org = Organization(name=DEMO_ORG)
            db.add(org)
            db.flush()
            db.add(Membership(user_id=user.id, org_id=org.id, role=Role.OWNER.label))
            created = True
        project = db.scalar(
            select(Project).where(Project.org_id == org.id, Project.name == DEMO_PROJECT)
        )
        if project is None:
            project = Project(org_id=org.id, name=DEMO_PROJECT)
            db.add(project)
            db.flush()
            created = True
        key = db.scalar(select(ApiKey).where(ApiKey.public_key == public_key))
        if key is None:
            db.add(
                ApiKey(
                    project_id=project.id,
                    public_key=public_key,
                    secret_hash=hash_secret(secret_key, settings.secret_key),
                    note=DEMO_KEY_NOTE,
                )
            )
            created = True
        elif key.revoked_at is not None:
            key.revoked_at = None  # the demo key is meant to be usable
        db.commit()
        return DemoInfo(email, password, org.id, project.id, public_key, secret_key, created)
