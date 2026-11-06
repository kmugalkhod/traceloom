import logging
import time

import pytest
from fastapi import Response

from traceloom.auth import csrf
from traceloom.auth.api_keys import (
    ApiKeyCache,
    ResolvedKey,
    generate_key_pair,
    hash_secret,
    verify_secret,
)
from traceloom.auth.passwords import WeakPassword, hash_password, verify_password
from traceloom.auth.sessions import (
    COOKIE_NAME,
    issue_session,
    read_session,
    set_session_cookie,
)
from traceloom.config import Settings
from traceloom.services.mail import Mailer

SERVER_KEY = "server-key-for-tests"


@pytest.fixture
def settings():
    return Settings(TRACELOOM_ENV="test", TRACELOOM_SERVER_SECRET=SERVER_KEY)


# --- passwords ---


def test_short_passwords_rejected():
    with pytest.raises(WeakPassword):
        hash_password("123456789")  # 9 chars
    assert hash_password("1234567890")  # 10 is the minimum


def test_hash_verifies_and_is_not_plaintext():
    h = hash_password("correct horse battery")
    assert "correct horse" not in h
    assert verify_password(h, "correct horse battery")
    assert not verify_password(h, "wrong password!!")
    assert not verify_password("not-a-hash", "whatever-password")


# --- sessions ---


def test_session_round_trip(settings):
    token = issue_session(settings, "u1", 3)
    data = read_session(settings, token, current_version=3)
    assert (data.user_id, data.session_version) == ("u1", 3)


def test_tampered_session_rejected(settings):
    token = issue_session(settings, "u1", 1)
    forged = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert read_session(settings, forged) is None
    assert read_session(settings, None) is None
    other = Settings(TRACELOOM_ENV="test", TRACELOOM_SERVER_SECRET="another-key")
    assert read_session(other, token) is None


def test_old_session_version_rejected(settings):
    token = issue_session(settings, "u1", 1)
    assert read_session(settings, token, current_version=2) is None


def test_expired_session_rejected(settings):
    token = issue_session(settings, "u1", 1)
    time.sleep(1.1)
    short = Settings(
        TRACELOOM_ENV="test", TRACELOOM_SERVER_SECRET=SERVER_KEY, TRACELOOM_SESSION_HOURS=0
    )
    assert read_session(short, token) is None


def test_session_cookie_flags():
    prod = Settings(TRACELOOM_ENV="production", TRACELOOM_SERVER_SECRET=SERVER_KEY)
    response = Response()
    set_session_cookie(response, prod, "tok")
    header = response.headers["set-cookie"].lower()
    assert header.startswith(f"{COOKIE_NAME}=tok")
    assert "httponly" in header and "secure" in header and "samesite=lax" in header


# --- csrf tokens ---


def test_csrf_token_verify():
    token = csrf.issue_token(SERVER_KEY)
    assert csrf.verify_token(SERVER_KEY, token)
    assert not csrf.verify_token("other", token)
    assert not csrf.verify_token(SERVER_KEY, token + "x")
    assert not csrf.verify_token(SERVER_KEY, None)
    assert csrf.tokens_match(SERVER_KEY, token, token)
    assert not csrf.tokens_match(SERVER_KEY, token, csrf.issue_token(SERVER_KEY))


# --- api keys ---


def test_key_pair_format_and_hashed_storage():
    pk, sk = generate_key_pair()
    assert pk.startswith("tl-pk-") and sk.startswith("tl-sk-")
    assert generate_key_pair() != (pk, sk)
    stored = hash_secret(sk, SERVER_KEY)
    assert sk not in stored and stored != sk
    assert verify_secret(sk, stored, SERVER_KEY)
    assert not verify_secret(sk + "x", stored, SERVER_KEY)
    assert not verify_secret(sk, stored, "other-server-key")


def test_cache_evict_fails_key_immediately():
    cache = ApiKeyCache(ttl_seconds=60)
    key = ResolvedKey("k1", "p1", "tl-pk-a")
    cache.put("tl-pk-a", "h", key)
    cache.put("tl-pk-b", "h2", ResolvedKey("k2", "p1", "tl-pk-b"))
    assert cache.get("tl-pk-a", "h") == key
    cache.evict("tl-pk-a")
    assert cache.get("tl-pk-a", "h") is None
    assert cache.get("tl-pk-b", "h2") is not None


def test_cache_ttl_expiry():
    now = [0.0]
    cache = ApiKeyCache(ttl_seconds=5, clock=lambda: now[0])
    cache.put("pk", "h", ResolvedKey("k", "p", "pk"))
    now[0] = 6
    assert cache.get("pk", "h") is None


# --- mail ---


def test_outbox_captures_messages(settings):
    mailer = Mailer(settings)
    assert mailer.send("a@example.com", "Hello", "Body text")
    assert [(m.to, m.subject, m.body) for m in mailer.outbox] == [
        ("a@example.com", "Hello", "Body text")
    ]


def test_console_fallback_never_logs_secrets(caplog):
    s = Settings(
        TRACELOOM_ENV="test",
        TRACELOOM_SERVER_SECRET=SERVER_KEY,
        SMTP_PASSWORD="smtp-pass-123",
    )
    mailer = Mailer(s)
    with caplog.at_level(logging.INFO, logger="traceloom.mail"):
        mailer.send(
            "a@example.com", "Your key", "secret tl-sk-abc123_XYZ and smtp-pass-123 " + SERVER_KEY
        )
    logged = caplog.text
    assert "a@example.com" in logged
    for secret in ("tl-sk-abc123_XYZ", "smtp-pass-123", SERVER_KEY):
        assert secret not in logged
    assert "tl-sk-abc123_XYZ" in mailer.outbox[0].body  # the real message is untouched
