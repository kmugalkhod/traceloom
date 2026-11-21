"""Phase 3 permission sweep: every new write route refuses a Viewer, and every Admin route
refuses a Member. Routes are read from the OpenAPI schema, so a route added later is covered."""

from __future__ import annotations

import pytest

from traceloom.auth.permissions import Role

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
HTML = {"Accept": "text/html"}
# Phase 3 areas of the web app (project-scoped pages and their writes).
PHASE3_PREFIXES = (
    "/datasets",
    "/evals",
    "/queues",
    "/settings/evaluation",
    "/settings/scores",
    "/traces/",  # per-trace score writes
)
# Paths that need an Admin (everything else under the prefixes needs Member).
ADMIN_ONLY = (
    "/evals/rules",
    "/settings/evaluation",
    "/settings/scores",
    "/queues/{queue_id}/delete",
    "/datasets/{dataset_id}/delete",
)


def write_routes(app):
    out = []
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/project/{project_id}"):
            continue
        rest = path[len("/project/{project_id}") :]
        if not rest.startswith(PHASE3_PREFIXES):
            continue
        if rest.startswith("/traces/") and not rest.endswith("/scores"):
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
    assert found, "no Phase 3 write routes found: the sweep would pass vacuously"
    return found


def test_the_sweep_sees_the_phase3_routes(routes):
    paths = {path for _, path, _ in routes}
    base = "/project/{project_id}"
    for expected in (
        f"{base}/datasets",
        f"{base}/queues",
        f"{base}/evals/rules",
        f"{base}/evals/templates",
        f"{base}/settings/evaluation/cap",
        f"{base}/settings/evaluation/origins",
        f"{base}/settings/scores",
        f"{base}/evals/log/{{execution_id}}/retry",
    ):
        assert expected in paths, expected
    assert len(routes) >= 30


def call(client, method, path, project_id):
    return client.request(method, fill(path, project_id), json={}, headers=HTML)


def test_viewer_gets_403_on_every_new_write(client, login, world, routes):
    login(client, world.users[Role.VIEWER])
    for method, path, _ in routes:
        r = call(client, method, path, world.project.id)
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


def test_member_gets_403_on_every_admin_route(client, login, world, routes):
    login(client, world.users[Role.MEMBER])
    admin_routes = [(m, p, rest) for m, p, rest in routes if needs_admin(rest)]
    assert len(admin_routes) >= 12
    for method, path, _ in admin_routes:
        r = call(client, method, path, world.project.id)
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


def test_member_is_not_refused_on_member_routes(client, login, world, routes):
    """Guards the sweep itself: a Member reaches the Member-level routes (and gets a validation
    error or not-found, never a permission error)."""
    login(client, world.users[Role.MEMBER])
    member_routes = [(m, p, rest) for m, p, rest in routes if not needs_admin(rest)]
    assert member_routes
    for method, path, _ in member_routes:
        r = call(client, method, path, world.project.id)
        assert r.status_code != 403, f"{method} {path} refused a Member"


def test_admin_is_not_refused_on_admin_routes(client, login, world, routes):
    login(client, world.users[Role.ADMIN])
    for method, path, rest in routes:
        if needs_admin(rest):
            r = call(client, method, path, world.project.id)
            assert r.status_code != 403, f"{method} {path} refused an Admin"


def test_public_score_api_needs_a_key(client, world):
    assert client.post(
        "/api/public/scores", json={"traceId": "t", "name": "n", "value": 1}
    ).status_code in (401, 403)
    assert client.delete("/api/public/scores/x").status_code in (401, 403)
