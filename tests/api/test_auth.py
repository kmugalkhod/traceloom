from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from traceloom.auth.sessions import COOKIE_NAME
from traceloom.config import Settings
from traceloom.db.models import Invite, Membership, PasswordReset, User
from traceloom.main import create_app
from traceloom.services.accounts import hash_token

GOOD = "correct horse battery"


def prime(client):
    """Anonymous visitors get a CSRF cookie on their first page; forms echo it."""
    client.get("/login")
    client.headers["X-CSRF-Token"] = client.cookies["tl_csrf"]
    return client


def signup(client, email="new@example.com", password=GOOD, name="New Person", **extra):
    return client.post(
        "/signup",
        data={"email": email, "password": password, "name": name, **extra},
        follow_redirects=False,
    )


def login(client, email, password=GOOD, **extra):
    return client.post(
        "/login", data={"email": email, "password": password, **extra}, follow_redirects=False
    )


def reset_token(container):
    mail = container.mailer.outbox[-1]
    return re.search(r"/reset/([\w-]+)", mail.body).group(1)


# --- sign-up ---


def test_signup_creates_account_and_signs_in(client, container, db):
    prime(client)
    r = signup(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert COOKIE_NAME in r.cookies
    user = db.scalar(select(User).where(User.email == "new@example.com"))
    assert user.name == "New Person" and user.password_hash.startswith("$argon2")
    assert GOOD not in user.password_hash
    assert client.get("/login", follow_redirects=False).status_code == 303  # signed in now


def test_short_password_is_an_inline_error(client, db):
    prime(client)
    r = signup(client, password="1234567")
    assert r.status_code == 400
    assert "Use at least 10 characters. You entered 7." in r.text
    assert 'aria-invalid="true"' in r.text and 'role="alert"' in r.text
    assert "New Person" in r.text  # what they typed is kept
    assert db.scalar(select(User).where(User.email == "new@example.com")) is None


@pytest.mark.parametrize("email", ["nope", "a@", ""])
def test_invalid_email_is_rejected(client, email):
    prime(client)
    assert "valid email" in signup(client, email=email).text


def test_duplicate_email_is_rejected_case_insensitively(client, make_user):
    make_user(email="taken@example.com")
    prime(client)
    r = signup(client, email="Taken@Example.com")
    assert r.status_code == 400 and "already exists" in r.text


# --- log in / out ---


def test_login_success_and_logout(client, make_user, db):
    make_user(email="a@example.com", password_hash=None)
    from tests.factories import DEFAULT_PASSWORD

    prime(client)
    r = login(client, "A@Example.com", DEFAULT_PASSWORD)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/login", follow_redirects=False).status_code == 303
    out = client.post("/logout", follow_redirects=False)
    assert out.status_code == 303 and out.headers["location"] == "/login"
    client.cookies.delete(COOKIE_NAME)  # the browser drops the cleared cookie
    assert client.get("/login", follow_redirects=False).status_code == 200


def test_wrong_password_and_unknown_email_look_the_same(client, make_user):
    make_user(email="a@example.com")
    prime(client)
    wrong = login(client, "a@example.com", "not the password")
    unknown = login(client, "nobody@example.com", "not the password")
    assert wrong.status_code == unknown.status_code == 401
    assert "The email or password is incorrect." in wrong.text
    strip = lambda t: re.sub(r'value="[^"]*"', "", t)  # noqa: E731
    assert strip(wrong.text) == strip(unknown.text)


@pytest.mark.parametrize(
    ("nxt", "expected"),
    [
        ("/project/abc/traces?q=1", "/project/abc/traces?q=1"),
        ("//evil.example", "/"),
        ("https://evil.example", "/"),
        ("/\\evil.example", "/"),
        ("", "/"),
    ],
)
def test_next_is_only_followed_for_local_paths(client, make_user, nxt, expected):
    make_user(email="a@example.com")
    from tests.factories import DEFAULT_PASSWORD

    prime(client)
    assert (
        login(client, "a@example.com", DEFAULT_PASSWORD, next=nxt).headers["location"] == expected
    )


def test_login_requires_a_csrf_token(client):
    r = client.post("/login", data={"email": "a@example.com", "password": GOOD})
    assert r.status_code == 403


def test_11th_login_attempt_in_a_minute_is_429(client):
    prime(client)
    codes = [login(client, "a@example.com", "wrong password").status_code for _ in range(11)]
    assert codes == [401] * 10 + [429]
    r = login(client, "a@example.com", "wrong password")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert "Too many attempts" in r.text
    assert login(client, "other@example.com", "wrong password").status_code == 401  # per IP + email


# --- password reset ---


def test_reset_mail_is_captured_and_reply_is_identical_for_unknown_emails(
    client, container, make_user
):
    make_user(email="a@example.com")
    prime(client)
    known = client.post("/forgot", data={"email": "a@example.com"})
    assert len(container.mailer.outbox) == 1
    mail = container.mailer.outbox[0]
    assert mail.to == "a@example.com" and "/reset/" in mail.body
    unknown = client.post("/forgot", data={"email": "ghost@example.com"})
    garbage = client.post("/forgot", data={"email": "not an email"})
    assert len(container.mailer.outbox) == 1  # nothing sent to strangers
    strip = lambda t: re.sub(r'value="[^"]*"', "", t)  # noqa: E731
    assert known.status_code == unknown.status_code == garbage.status_code == 200
    assert strip(known.text) == strip(unknown.text) == strip(garbage.text)
    assert "Check your inbox" in known.text


def test_reset_token_is_stored_hashed(client, container, make_user, db):
    make_user(email="a@example.com")
    prime(client)
    client.post("/forgot", data={"email": "a@example.com"})
    token = reset_token(container)
    row = db.scalar(select(PasswordReset))
    assert row.token_hash == hash_token(token) and token not in row.token_hash


def test_reset_flow_sets_password_and_token_is_single_use(client, container, make_user):
    make_user(email="a@example.com")
    prime(client)
    client.post("/forgot", data={"email": "a@example.com"})
    token = reset_token(container)
    assert client.get(f"/reset/{token}").status_code == 200
    mismatch = client.post(
        f"/reset/{token}", data={"password": "brand new password", "password2": "different one!!"}
    )
    assert mismatch.status_code == 400 and "do not match" in mismatch.text
    weak = client.post(f"/reset/{token}", data={"password": "short", "password2": "short"})
    assert weak.status_code == 400 and "at least 10" in weak.text
    ok = client.post(
        f"/reset/{token}",
        data={"password": "brand new password", "password2": "brand new password"},
        follow_redirects=False,
    )
    assert ok.status_code == 303 and ok.headers["location"] == "/login?reset=1"
    assert login(client, "a@example.com", "brand new password").status_code == 303
    assert login(client, "a@example.com", GOOD).status_code == 401
    again = client.post(
        f"/reset/{token}", data={"password": "yet another one!", "password2": "yet another one!"}
    )
    assert again.status_code == 410 and "expired or was already used" in again.text
    assert client.get(f"/reset/{token}").status_code == 410


def test_expired_and_unknown_tokens_fail(client, container, make_user, db):
    make_user(email="a@example.com")
    prime(client)
    client.post("/forgot", data={"email": "a@example.com"})
    token = reset_token(container)
    row = db.scalar(select(PasswordReset))
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert client.get(f"/reset/{token}").status_code == 410
    assert client.get("/reset/not-a-token").status_code == 410


def test_reset_token_lifetime_comes_from_settings(settings, tmp_path):
    from fastapi.testclient import TestClient

    short = Settings(**{**settings.model_dump(by_alias=False), "reset_token_minutes": 5})
    with TestClient(create_app(short)) as c:
        c.get("/login")
        c.headers["X-CSRF-Token"] = c.cookies["tl_csrf"]
        sf = c.app.state.container.session_factory
        with sf.begin() as s:
            s.add(User(email="a@example.com", password_hash="x"))
        body = c.post("/forgot", data={"email": "a@example.com"}).text
        assert "5 minutes" in body
        with sf() as s:
            row = s.scalar(select(PasswordReset))
            assert timedelta(minutes=4) < row.expires_at - datetime.now(UTC) <= timedelta(minutes=5)


def test_reset_invalidates_sessions(client, container, make_user, db):
    from fastapi.testclient import TestClient

    from tests.factories import DEFAULT_PASSWORD

    make_user(email="a@example.com")
    prime(client)
    assert login(client, "a@example.com", DEFAULT_PASSWORD).status_code == 303
    # A second browser, signed in as the same person.
    other = TestClient(client.app)
    other.__enter__()
    prime(other)
    assert login(other, "a@example.com", DEFAULT_PASSWORD).status_code == 303
    assert other.get("/login", follow_redirects=False).status_code == 303
    client.post("/forgot", data={"email": "a@example.com"})
    token = reset_token(container)
    client.post(
        f"/reset/{token}",
        data={"password": "brand new password", "password2": "brand new password"},
    )
    for browser in (client, other):  # every session is gone, including the one that did the reset
        assert browser.get("/login", follow_redirects=False).status_code == 200
    other.__exit__(None, None, None)


# --- invite-only sign-up ---


@pytest.fixture
def closed(settings):
    from fastapi.testclient import TestClient

    closed = Settings(**{**settings.model_dump(by_alias=False), "signup_enabled": False})
    with TestClient(create_app(closed)) as c:
        yield prime(c)


def add_invite(client, org, email="invited@example.com", role="member", token="tok-123", hours=24):
    with client.app.state.container.session_factory.begin() as s:
        s.add(
            Invite(
                org_id=org.id,
                email=email,
                role=role,
                token_hash=hash_token(token),
                expires_at=datetime.now(UTC) + timedelta(hours=hours),
            )
        )


def test_signup_disabled_shows_the_invite_only_form(closed):
    page = closed.get("/signup").text
    assert (
        "invite-only" in page and "Continue with invite" in page and 'name="password"' not in page
    )


def test_signup_disabled_rejects_uninvited_emails(closed, make_org):
    r = closed.post("/signup", data={"email": "stranger@example.com"})
    assert r.status_code == 400 and "No invite was found" in r.text
    r = closed.post(
        "/signup", data={"email": "stranger@example.com", "password": GOOD, "name": "S"}
    )
    assert r.status_code == 400 and "invite-only" in r.text
    with closed.app.state.container.session_factory() as s:
        assert s.scalar(select(User).where(User.email == "stranger@example.com")) is None


def test_signup_disabled_lets_an_invited_email_register(closed, make_org):
    org = make_org()
    add_invite(closed, org)
    step = closed.post("/signup", data={"email": "Invited@example.com"})
    assert (
        step.status_code == 200
        and 'name="password"' in step.text
        and "invited@example.com" in step.text
    )
    r = closed.post(
        "/signup",
        data={"email": "invited@example.com", "password": GOOD, "name": "I"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with closed.app.state.container.session_factory() as s:
        user = s.scalar(select(User).where(User.email == "invited@example.com"))
        assert s.get(Membership, (user.id, org.id)).role == "member"
        assert s.scalar(select(Invite)).accepted_at is not None


def test_invite_link_signup_joins_the_org_with_the_invited_role(closed, make_org):
    org = make_org()
    add_invite(closed, org, role="admin")
    page = closed.get("/signup?invite=tok-123").text
    assert (
        'name="password"' in page and 'value="invited@example.com"' in page and "readonly" in page
    )
    r = closed.post(
        "/signup",
        data={
            "email": "someone-else@example.com",
            "password": GOOD,
            "name": "I",
            "invite": "tok-123",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    with closed.app.state.container.session_factory() as s:
        user = s.scalar(select(User))
        assert user.email == "invited@example.com"  # an invite is only good for its own address
        assert s.get(Membership, (user.id, org.id)).role == "admin"


def test_expired_invites_do_not_open_signup(closed, make_org):
    add_invite(closed, make_org(), hours=-1)
    assert (
        "No invite was found" in closed.post("/signup", data={"email": "invited@example.com"}).text
    )
