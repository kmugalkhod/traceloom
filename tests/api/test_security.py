import pytest
from fastapi import APIRouter, FastAPI

from traceloom.auth.csrf import COOKIE_NAME as CSRF_COOKIE
from traceloom.auth.permissions import Role
from traceloom.config import Settings
from traceloom.main import create_app

echo = APIRouter()


@echo.post("/probe/echo")
def echo_post():
    return {"ok": True}


@echo.post("/api/public/probe/echo")
def echo_public():
    return {"ok": True}


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    app = create_app(settings)
    app.include_router(echo)
    return app


def test_unsafe_cookie_request_without_csrf_token_is_rejected(client, world, login):
    login(client, world.user(Role.ADMIN), csrf=False)
    r = client.post("/probe/echo")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf_failed"


def test_wrong_csrf_token_is_rejected(client, world, login):
    login(client, world.user(Role.ADMIN), csrf=False)
    client.get("/api/public/health")
    assert client.post("/probe/echo", headers={"X-CSRF-Token": "forged.token"}).status_code == 403
    other = client.cookies[CSRF_COOKIE][:-1] + "0"
    assert client.post("/probe/echo", headers={"X-CSRF-Token": other}).status_code == 403


def test_matching_header_token_is_accepted(client, world, login):
    login(client, world.user(Role.ADMIN))
    assert client.post("/probe/echo").status_code == 200


def test_form_field_token_is_accepted(client):
    client.get("/api/public/health")
    token = client.cookies[CSRF_COOKIE]
    assert client.post("/probe/echo", data={"csrf_token": token}).status_code == 200
    assert client.post("/probe/echo", data={"csrf_token": "nope"}).status_code == 403


def test_anonymous_post_also_needs_csrf(client):
    assert client.post("/probe/echo").status_code == 403  # login CSRF is closed too


def test_basic_auth_ingestion_paths_are_exempt(client):
    assert client.post("/api/public/probe/echo").status_code == 200


def test_get_requests_are_never_blocked_and_set_the_csrf_cookie(client):
    r = client.get("/api/public/health")
    assert r.status_code == 200
    assert CSRF_COOKIE in r.cookies


def test_csrf_cookie_flags(client):
    header = client.get("/api/public/health").headers["set-cookie"].lower()
    assert "samesite=lax" in header and "path=/" in header
    assert "secure" not in header  # dev/test only; production sets it


def test_session_and_csrf_cookies_are_secure_in_production(tmp_path):
    from fastapi.testclient import TestClient

    s = Settings(
        TRACELOOM_ENV="production",
        TRACELOOM_SECRET_KEY="p" * 32,
        DATABASE_URL=f"sqlite:///{tmp_path / 'p.db'}",
    )
    with TestClient(create_app(s)) as c:
        header = c.get("/api/public/health").headers["set-cookie"].lower()
        assert "secure" in header
