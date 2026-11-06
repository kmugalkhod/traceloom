"""Route-table permission sweep plus the require() / Basic key / error-envelope behaviour."""

from __future__ import annotations

import base64
import re

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient

from traceloom.auth.api_keys import hash_secret
from traceloom.auth.deps import (
    Access,
    current_user,
    enforce_auth_rate_limit,
    project_key,
    require,
    revoke_api_key,
)
from traceloom.auth.permissions import Perm, Role
from traceloom.config import Settings
from traceloom.main import create_app

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
# Routes that are intentionally reachable without a project role: they authenticate another
# way (API key) or before a session exists.
# Routes any signed-in user may call, by design: creating your own organization.
OPEN_TO_SIGNED_IN_USERS = {("POST", "/orgs/new")}
PUBLIC_PREFIXES = ("/api/public/", "/login", "/signup", "/logout", "/forgot", "/reset", "/invite")

probe = APIRouter()


@probe.get("/probe/projects/{project_id}/read")
def probe_read(access: Access = Depends(require(Perm.TRACES_READ))):
    return {"role": access.role.label}


@probe.post("/probe/projects/{project_id}/annotate")
def probe_annotate(access: Access = Depends(require(Perm.ANNOTATE))):
    return {"ok": True}


@probe.delete("/probe/projects/{project_id}/traces")
def probe_delete(access: Access = Depends(require(Perm.TRACES_DELETE))):
    return {"ok": True}


@probe.post("/probe/orgs/{org_id}/owner-only")
def probe_owner(access: Access = Depends(require(Perm.ORG_DELETE))):
    return {"ok": True}


@probe.get("/probe/me")
def probe_me(user=Depends(current_user)):
    return {"id": user.id}


@probe.get("/api/public/probe/keyed/{project_id}")
def probe_keyed(project_id: str, key=Depends(project_key)):
    return {"project_id": key.project_id}


@probe.get("/api/public/probe/key")
def probe_key(key=Depends(project_key)):
    return {"project_id": key.project_id}


@probe.post("/probe/auth-limited")
def probe_limited(request: Request, email: str = "a@example.com"):
    enforce_auth_rate_limit(request, email)
    return {"ok": True}


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    app = create_app(settings)
    app.include_router(probe)
    return app


def basic(public: str, secret: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()}


# --- sweep over the route table ---------------------------------------------------------------


@probe.post("/probe/projects/{project_id}/unguarded")
def probe_unguarded():
    return {"ok": True}


def unsafe_routes(app) -> list[tuple[str, str]]:
    """(METHOD, path template) for every unsafe route, read from the OpenAPI schema.

    The schema is the public view of the route table (FastAPI nests included routers in
    private structures), so routes declared with include_in_schema=False are not swept.
    """
    return [
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if method.upper() in UNSAFE
    ]


def sweep_viewer(app, client, world, skip_prefixes=()) -> list[tuple[str, str, int]]:
    """Call every unsafe route as a Viewer; returns (method, path template, status)."""
    ids = {"project_id": world.project.id, "org_id": world.org.id}
    results = []
    for method, template in unsafe_routes(app):
        if (
            template.startswith((*PUBLIC_PREFIXES, *skip_prefixes))
            or (method, template) in OPEN_TO_SIGNED_IN_USERS
        ):
            continue
        path = re.sub(r"\{(\w+)\}", lambda m: ids.get(m[1], "x"), template)
        results.append((method, template, client.request(method, path, json={}).status_code))
    return results


def test_every_unsafe_route_forbids_a_viewer(app, client, world, login):
    """Sweeps whatever routes exist; later stories' routes are picked up automatically."""
    login(client, world.user(Role.VIEWER))
    results = sweep_viewer(app, client, world, skip_prefixes=("/probe",))
    assert [r for r in results if r[2] != 403] == []


def test_sweep_flags_an_unguarded_route(app, client, world, login):
    login(client, world.user(Role.VIEWER))
    results = sweep_viewer(app, client, world)
    not_forbidden = {(m, p) for m, p, status in results if status != 403}
    assert not_forbidden == {
        ("POST", "/probe/projects/{project_id}/unguarded"),
        ("POST", "/probe/auth-limited"),
    }
    assert ("POST", "/probe/projects/{project_id}/annotate", 403) in results
    assert ("DELETE", "/probe/projects/{project_id}/traces", 403) in results
    assert ("POST", "/probe/orgs/{org_id}/owner-only", 403) in results


# --- authentication ---------------------------------------------------------------------------


def test_api_unauthenticated_is_401_envelope(client, world):
    r = client.get(
        f"/probe/projects/{world.project.id}/read", headers={"Accept": "application/json"}
    )
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "unauthorized", "message": "Authentication required."}}


def test_page_unauthenticated_redirects_to_login(client, world):
    r = client.get(f"/probe/projects/{world.project.id}/read", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=")


def test_tampered_or_stale_session_is_unauthenticated(client, world, login, db):
    user = world.user(Role.MEMBER)
    login(client, user)
    assert client.get("/probe/me").status_code == 200
    user_row = db.get(type(user), user.id)
    user_row.session_version += 1  # e.g. password reset
    db.commit()
    assert client.get("/probe/me", follow_redirects=False).status_code == 303
    client.cookies.set("tl_session", "garbage")
    assert client.get("/probe/me", follow_redirects=False).status_code == 303


# --- authorization ----------------------------------------------------------------------------

ROLE_CASES = [
    # (method, path template, minimal role allowed)
    ("GET", "/probe/projects/{p}/read", Role.VIEWER),
    ("POST", "/probe/projects/{p}/annotate", Role.MEMBER),
    ("DELETE", "/probe/projects/{p}/traces", Role.ADMIN),
    ("POST", "/probe/orgs/{o}/owner-only", Role.OWNER),
]


@pytest.mark.parametrize(("method", "template", "minimum"), ROLE_CASES)
@pytest.mark.parametrize("role", list(Role))
def test_role_gate(client, world, login, method, template, minimum, role):
    login(client, world.user(role))
    path = template.format(p=world.project.id, o=world.org.id)
    r = client.request(method, path, headers={"Accept": "application/json"})
    if role >= minimum:
        assert r.status_code == 200
    else:
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "forbidden"


def test_non_member_gets_404_not_403(client, world, make_user, login):
    login(client, make_user())
    r = client.get(
        f"/probe/projects/{world.project.id}/read", headers={"Accept": "application/json"}
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_unknown_route_uses_envelope_for_api_paths(client):
    r = client.get("/api/public/nope")
    assert r.status_code == 404
    assert set(r.json()["error"]) == {"code", "message"}


# --- project API keys -------------------------------------------------------------------------


def test_basic_key_resolves_to_its_project_only(client, world, make_key, make_project):
    _, pk, sk = make_key(world.project)
    other = make_project(world.org)
    r = client.get("/api/public/probe/key", headers=basic(pk, sk))
    assert r.json() == {"project_id": world.project.id}
    assert (
        client.get(f"/api/public/probe/keyed/{world.project.id}", headers=basic(pk, sk)).status_code
        == 200
    )
    denied = client.get(f"/api/public/probe/keyed/{other.id}", headers=basic(pk, sk))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("mode", ["wrong-secret", "unknown-key", "no-header", "malformed"])
def test_bad_credentials_are_401(client, world, make_key, mode):
    _, pk, sk = make_key(world.project)
    headers = {
        "wrong-secret": basic(pk, sk + "x"),
        "unknown-key": basic("tl-pk-nope", sk),
        "no-header": {},
        "malformed": {"Authorization": "Basic !!!"},
    }[mode]
    r = client.get("/api/public/probe/key", headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")


def test_key_use_updates_last_used_at(client, world, make_key, db):
    key, pk, sk = make_key(world.project)
    assert key.last_used_at is None
    client.get("/api/public/probe/key", headers=basic(pk, sk))
    db.refresh(key)
    assert key.last_used_at is not None


def test_revoked_key_fails_immediately_despite_cache(client, container, world, make_key, db):
    key, pk, sk = make_key(world.project)
    assert client.get("/api/public/probe/key", headers=basic(pk, sk)).status_code == 200
    assert container.api_key_cache.get(
        pk,
        hash_secret(sk, container.settings.secret_key),
    )
    assert revoke_api_key(db, container, key.id)
    db.commit()
    assert client.get("/api/public/probe/key", headers=basic(pk, sk)).status_code == 401


# --- auth rate limit hook ---------------------------------------------------------------------


def test_auth_rate_limit_hook_returns_429(app, settings):
    settings_limited = Settings(
        TRACELOOM_ENV="test",
        TRACELOOM_SERVER_SECRET="k" * 20,
        TRACELOOM_RATE_AUTH="2/minute",
        DATABASE_URL=settings.database_url,
    )
    app2 = create_app(settings_limited)
    app2.include_router(probe)
    with TestClient(app2) as c:
        c.get("/api/public/health")
        h = {"X-CSRF-Token": c.cookies["tl_csrf"], "Accept": "application/json"}
        codes = [
            c.post("/probe/auth-limited?email=a@x.io", headers=h).status_code for _ in range(3)
        ]
        assert codes == [200, 200, 429]
        assert c.post("/probe/auth-limited?email=b@x.io", headers=h).status_code == 200
