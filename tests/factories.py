"""Factories and logged-in client helpers, loaded as a pytest plugin from conftest.py."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from itertools import count

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from traceloom.auth.api_keys import generate_key_pair, hash_secret
from traceloom.auth.csrf import COOKIE_NAME as CSRF_COOKIE
from traceloom.auth.passwords import hash_password
from traceloom.auth.permissions import Role
from traceloom.auth.sessions import COOKIE_NAME as SESSION_COOKIE
from traceloom.auth.sessions import issue_session
from traceloom.container import Container
from traceloom.db.models import ApiKey, Membership, Organization, Project, User

DEFAULT_PASSWORD = "correct horse battery"
_password_hash: str | None = None
_seq = count(1)


def default_password_hash() -> str:
    """Hash once per process: argon2 is deliberately slow."""
    global _password_hash
    if _password_hash is None:
        _password_hash = hash_password(DEFAULT_PASSWORD)
    return _password_hash


@dataclass
class World:
    """An org with one project and one user per role."""

    org: Organization
    project: Project
    users: dict[Role, User] = field(default_factory=dict)

    def user(self, role: Role) -> User:
        return self.users[role]


@pytest.fixture
def container(app: FastAPI) -> Container:
    return app.state.container


@pytest.fixture
def db(container: Container, client: TestClient) -> Iterator[Session]:
    """A session on the app's database (depends on `client` so migrations have run)."""
    with container.session_factory() as session:
        yield session


@pytest.fixture
def make_user(db: Session) -> Callable[..., User]:
    def make(email: str | None = None, name: str = "", password_hash: str | None = None) -> User:
        n = next(_seq)
        user = User(
            email=email or f"user{n}@example.com",
            name=name or f"User {n}",
            password_hash=password_hash or default_password_hash(),
        )
        db.add(user)
        db.commit()
        return user

    return make


@pytest.fixture
def make_org(db: Session) -> Callable[..., Organization]:
    def make(name: str | None = None) -> Organization:
        org = Organization(name=name or f"Org {next(_seq)}")
        db.add(org)
        db.commit()
        return org

    return make


@pytest.fixture
def make_project(db: Session) -> Callable[..., Project]:
    def make(org: Organization, name: str | None = None) -> Project:
        project = Project(org_id=org.id, name=name or f"Project {next(_seq)}")
        db.add(project)
        db.commit()
        return project

    return make


@pytest.fixture
def make_membership(db: Session) -> Callable[..., Membership]:
    def make(user: User, org: Organization, role: Role = Role.MEMBER) -> Membership:
        membership = Membership(user_id=user.id, org_id=org.id, role=role.label)
        db.add(membership)
        db.commit()
        return membership

    return make


@pytest.fixture
def make_key(db: Session, container: Container) -> Callable[..., tuple[ApiKey, str, str]]:
    """Returns (row, public_key, secret_key); the secret is only known here."""

    def make(project: Project, note: str = "test") -> tuple[ApiKey, str, str]:
        public, secret = generate_key_pair()
        key = ApiKey(
            project_id=project.id,
            public_key=public,
            secret_hash=hash_secret(secret, container.settings.secret_key),
            note=note,
        )
        db.add(key)
        db.commit()
        return key, public, secret

    return make


@pytest.fixture
def world(make_user, make_org, make_project, make_membership) -> World:
    org = make_org()
    world = World(org=org, project=make_project(org))
    for role in Role:
        user = make_user(email=f"{role.label}-{next(_seq)}@example.com")
        make_membership(user, org, role)
        world.users[role] = user
    return world


@pytest.fixture
def login(container: Container) -> Callable[..., TestClient]:
    """login(client, user, csrf=True): sign `client` in as `user`.

    With csrf=True the client also sends the matching X-CSRF-Token header on every request,
    like app.js does. Pass csrf=False to exercise the rejection path.
    """

    def do_login(client: TestClient, user: User, csrf: bool = True) -> TestClient:
        token = issue_session(container.settings, user.id, user.session_version)
        client.cookies.set(SESSION_COOKIE, token)
        if csrf:
            client.get("/api/public/health")  # the middleware issues the csrf cookie
            client.headers["X-CSRF-Token"] = client.cookies[CSRF_COOKIE]
        return client

    return do_login
