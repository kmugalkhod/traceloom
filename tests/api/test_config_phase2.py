from __future__ import annotations

import logging

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from traceloom.config import Settings

SECRET = "test-secret-key-not-for-production"


def test_phase2_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TRACELOOM_WEBHOOK_ALLOW_PRIVATE",
        "TRACELOOM_LLM_ALLOW_PRIVATE",
        "TRACELOOM_PLAYGROUND_RATE",
        "TRACELOOM_PROVIDER_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    s = Settings(TRACELOOM_ENV="test", TRACELOOM_SERVER_SECRET=SECRET)
    assert s.webhook_allow_private is False
    assert s.llm_allow_private is True
    assert s.playground_rate == "30/minute"
    assert s.provider_timeout_seconds == 120


def test_overrides_from_env() -> None:
    s = Settings(
        TRACELOOM_ENV="test",
        TRACELOOM_SERVER_SECRET=SECRET,
        TRACELOOM_WEBHOOK_ALLOW_PRIVATE="true",
        TRACELOOM_LLM_ALLOW_PRIVATE="false",
        TRACELOOM_PLAYGROUND_RATE="5/minute",
        TRACELOOM_PROVIDER_TIMEOUT_SECONDS="30",
    )
    assert s.webhook_allow_private is True
    assert s.llm_allow_private is False
    assert s.playground_rate == "5/minute"
    assert s.provider_timeout_seconds == 30


@pytest.mark.parametrize("env", ["test", "production"])
def test_startup_fails_outside_dev_without_key(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    monkeypatch.delenv("TRACELOOM_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValidationError, match="TRACELOOM_ENCRYPTION_KEY"):
        Settings(TRACELOOM_ENV=env, TRACELOOM_SERVER_SECRET=SECRET, _env_file=None)


def test_blank_key_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACELOOM_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValidationError, match="TRACELOOM_ENCRYPTION_KEY"):
        Settings(
            TRACELOOM_ENV="production",
            TRACELOOM_SERVER_SECRET=SECRET,
            TRACELOOM_ENCRYPTION_KEY=" , ",
            _env_file=None,
        )


def test_dev_generates_ephemeral_key_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("TRACELOOM_ENCRYPTION_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="traceloom.config"):
        s = Settings(TRACELOOM_ENV="dev", _env_file=None)
    Fernet(s.encryption_key.encode())  # a valid Fernet key
    assert any("ephemeral" in r.getMessage() for r in caplog.records)


def test_configured_key_is_kept_without_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    key = f"{Fernet.generate_key().decode()},{Fernet.generate_key().decode()}"
    with caplog.at_level(logging.WARNING, logger="traceloom.config"):
        s = Settings(
            TRACELOOM_ENV="production",
            TRACELOOM_SERVER_SECRET=SECRET,
            TRACELOOM_ENCRYPTION_KEY=key,
            _env_file=None,
        )
    assert s.encryption_key == key
    assert not caplog.records
