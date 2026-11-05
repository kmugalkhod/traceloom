"""FastAPI dependencies: current user, org/project access, permissions, API-key auth.

Every query behind a dependency is scoped through the resolved org/project. A user who is not a
member of a project's org gets 404, so project ids are not an existence oracle.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceloom.auth.api_keys import ResolvedKey, hash_secret
from traceloom.auth.permissions import Perm, Role, can
from traceloom.auth.sessions import SessionData
from traceloom.container import Container
from traceloom.db.models import ApiKey, Membership, Organization, Project, User
from traceloom.db.session import get_db


def get_container(request: Request) -> Container:
    return request.app.state.container


# --- Users --------------------------------------------------------------------------------


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    session: SessionData | None = getattr(request.state, "session", None)
    if session is None:
        return None
    user = db.get(User, session.user_id)
    if user is None or session.session_version != user.session_version:
        return None
    return user


def current_user(user: User | None = Depends(current_user_optional)) -> User:
    if user is None:
        raise HTTPException(401, "Authentication required.")
    return user


# --- Org / project access -----------------------------------------------------------------


@dataclass
class Access:
    user: User
    org: Organization
    role: Role
    project: Project | None = None


def _role_in(db: Session, user: User, org_id: str) -> Role | None:
    membership = db.get(Membership, (user.id, org_id))
    return Role.parse(membership.role) if membership else None


def current_org(org_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    org = db.get(Organization, org_id)
    role = _role_in(db, user, org_id) if org else None
    if org is None or role is None:
        raise HTTPException(404, "Not found.")
    return Access(user=user, org=org, role=role)


def current_project(
    project_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Access:
    project = db.get(Project, project_id)
    role = _role_in(db, user, project.org_id) if project else None
    if project is None or role is None:
        raise HTTPException(404, "Not found.")
    return Access(user=user, org=db.get(Organization, project.org_id), role=role, project=project)


def require(perm: Perm) -> Callable[..., Access]:
    """Dependency factory: resolves the project (`{project_id}` in the path) or the org
    (`{org_id}`), then enforces `perm` for the user's role there."""

    def dependency(
        request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
    ) -> Access:
        params = request.path_params
        if "project_id" in params:
            access = current_project(params["project_id"], user, db)
        elif "org_id" in params:
            access = current_org(params["org_id"], user, db)
        else:
            raise RuntimeError("require() needs {project_id} or {org_id} in the route path")
        if not can(access.role, perm):
            raise HTTPException(403, "You do not have permission to do this.")
        return access

    return dependency


# --- Project API keys (HTTP Basic) --------------------------------------------------------

LAST_USED_RESOLUTION = 60  # seconds


def _unauthorized() -> HTTPException:
    return HTTPException(
        401, "Invalid API key.", headers={"WWW-Authenticate": 'Basic realm="traceloom"'}
    )


def _parse_basic(header: str | None) -> tuple[str, str]:
    if not header or not header.lower().startswith("basic "):
        raise _unauthorized()
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise _unauthorized() from exc
    public_key, sep, secret = decoded.partition(":")
    if not sep or not public_key or not secret:
        raise _unauthorized()
    return public_key, secret


def project_key(
    request: Request,
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
) -> ResolvedKey:
    """Authenticate `Authorization: Basic pk:sk` to exactly one project.

    A route that also has `{project_id}` in its path gets 403 when the key belongs to a
    different project.
    """
    public_key, secret = _parse_basic(request.headers.get("authorization"))
    secret_hash = hash_secret(secret, container.settings.secret_key)
    resolved = container.api_key_cache.get(public_key, secret_hash)
    if resolved is None:
        key = db.scalar(select(ApiKey).where(ApiKey.public_key == public_key))
        stored = key.secret_hash if key else "0" * 64
        matches = hmac.compare_digest(secret_hash, stored)  # constant time even for unknown keys
        if key is None or not matches or key.revoked_at is not None:
            raise _unauthorized()
        now = datetime.now(UTC)
        if (
            key.last_used_at is None
            or (now - key.last_used_at).total_seconds() > LAST_USED_RESOLUTION
        ):
            key.last_used_at = now
            db.flush()
        resolved = ResolvedKey(key.id, key.project_id, key.public_key)
        container.api_key_cache.put(public_key, secret_hash, resolved)
    path_project = request.path_params.get("project_id")
    if path_project is not None and path_project != resolved.project_id:
        raise HTTPException(403, "This API key belongs to a different project.")
    return resolved


def revoke_api_key(db: Session, container: Container, key_id: str) -> bool:
    """Mark a key revoked and evict it from the cache so it fails on the next request."""
    key = db.get(ApiKey, key_id)
    if key is None or key.revoked_at is not None:
        return False
    key.revoked_at = datetime.now(UTC)
    db.flush()
    container.api_key_cache.evict(key.public_key)
    return True


# --- Rate limits --------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def enforce_auth_rate_limit(request: Request, email: str) -> None:
    """Per IP + email limit for login, sign-up and reset requests (TRACELOOM_RATE_AUTH).

    Call it from auth routes before touching the password. The client IP is the direct peer;
    deployments behind a proxy should run uvicorn with --proxy-headers.
    """
    container = get_container(request)
    result = container.limiter.hit(
        f"auth:{_client_ip(request)}:{email.strip().lower()}", container.settings.rate_auth
    )
    if not result.allowed:
        raise HTTPException(
            429,
            "Too many attempts. Try again later.",
            headers={"Retry-After": str(max(1, round(result.retry_after)))},
        )


def enforce_ingest_rate_limit(request: Request, key: ResolvedKey) -> None:
    """Per API key limit for ingestion (TRACELOOM_RATE_INGEST)."""
    container = get_container(request)
    result = container.limiter.hit(f"ingest:{key.key_id}", container.settings.rate_ingest)
    if not result.allowed:
        raise HTTPException(
            429,
            "Rate limit exceeded.",
            headers={"Retry-After": str(max(1, round(result.retry_after)))},
        )
