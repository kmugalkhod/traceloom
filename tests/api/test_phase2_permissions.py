"""Phase 2 permission sweep: every new write route refuses a Viewer, and every Admin route
refuses a Member. Routes are found by walking the app, so a route added later is covered too."""

from __future__ import annotations

import pytest

from traceloom.auth.permissions import Role

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Phase 2 areas of the web app (project-scoped pages and their writes).
PHASE2_PREFIXES = (
    "/prompts",
    "/playground",
    "/settings/connections",
    "/settings/webhooks",
)
# Pieces of a path that need an Admin (everything else under the prefixes needs Member).
ADMIN_ONLY = ("/delete", "/settings/connections", "/settings/webhooks", "/protected")
# Routes a Member may call, but where parts of the action need an Admin (checked separately).
MEMBER_OK = {"/labels"}


def write_routes(app):
    """(method, path, rest) for every Phase 2 write route, read from the OpenAPI schema (the
    router tree of newer FastAPI versions is lazy, so app.routes cannot be walked)."""
    out = []
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/project/{project_id}"):
            continue
        rest = path[len("/project/{project_id}") :]
        if not rest.startswith(PHASE2_PREFIXES):
            continue
        for method in sorted(m.upper() for m in operations if m.upper() in WRITE_METHODS):
            out.append((method, path, rest))
    return out


def fill(path: str, project_id: str) -> str:
    out = path.replace("{project_id}", project_id)
    while "{" in out:
        start, end = out.index("{"), out.index("}")
        out = out[:start] + "x" + out[end + 1 :]
    return out


def needs_admin(rest: str) -> bool:
    return any(piece in rest for piece in ADMIN_ONLY)


@pytest.fixture
def routes(app):
    found = write_routes(app)
    assert found, "no Phase 2 write routes found: the sweep would pass vacuously"
    return found


def test_the_sweep_sees_the_prompt_routes(routes):
    paths = {path for _, path, _ in routes}
    assert "/project/{project_id}/prompts/new" in paths
    assert "/project/{project_id}/prompts/{name}/labels" in paths or any(
        p.endswith("/labels") for p in paths
    )
    assert any(p.endswith("/delete") for p in paths)


def test_viewer_gets_403_on_every_new_write(client, login, world, routes):
    login(client, world.users[Role.VIEWER])
    for method, path, _ in routes:
        r = client.request(
            method, fill(path, world.project.id), json={}, headers={"Accept": "text/html"}
        )
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


def test_member_gets_403_on_every_admin_route(client, login, world, routes):
    login(client, world.users[Role.MEMBER])
    admin_routes = [(m, p, rest) for m, p, rest in routes if needs_admin(rest)]
    assert any(p.endswith("/delete") for _, p, _ in admin_routes)
    for method, path, _ in admin_routes:
        r = client.request(
            method, fill(path, world.project.id), json={}, headers={"Accept": "text/html"}
        )
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


def test_member_is_not_refused_on_member_routes(client, login, world, routes):
    """Guards the sweep itself: a Member reaches the Member-level routes (and gets a validation
    error or not-found, never a permission error)."""
    login(client, world.users[Role.MEMBER])
    member_routes = [(m, p, rest) for m, p, rest in routes if not needs_admin(rest)]
    assert member_routes
    for method, path, _ in member_routes:
        r = client.request(
            method, fill(path, world.project.id), json={}, headers={"Accept": "text/html"}
        )
        assert r.status_code != 403, f"{method} {path} refused a Member"


def test_readers_can_open_pages_but_not_the_editor(client, login, world):
    login(client, world.users[Role.VIEWER])
    root = f"/project/{world.project.id}/prompts"
    assert client.get(root, headers={"Accept": "text/html"}).status_code == 200
    assert client.get(f"{root}/new", headers={"Accept": "text/html"}).status_code == 403


def test_member_gets_403_on_admin_pages(client, login, world):
    login(client, world.users[Role.MEMBER])
    base = f"/project/{world.project.id}/settings"
    for page in ("connections", "webhooks"):
        r = client.get(f"{base}/{page}", headers={"Accept": "text/html"})
        assert r.status_code in (403, 404), f"{page} -> {r.status_code}"


def test_protected_label_and_flagged_key_rules(client, login, world, db, make_key):
    """Members cannot move production in the UI; a key only can with the flag (API 403)."""
    from tests.api.test_prompt_detail_page import csrf, save, url

    save(db, world, content="x")
    login(client, world.users[Role.MEMBER])
    token = csrf(client, url(world))
    data = {"label": "production", "version": "1", "csrf_token": token}
    assert (
        client.post(
            url(world, suffix="/labels"), data=data, headers={"Accept": "text/html"}
        ).status_code
        == 403
    )
    data["label"] = "staging"
    assert (
        client.post(
            url(world, suffix="/labels"),
            data=data,
            headers={"Accept": "text/html"},
            follow_redirects=False,
        ).status_code
        == 303
    )
