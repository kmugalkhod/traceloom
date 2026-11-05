import pytest
from pydantic import ValidationError

from traceloom.config import Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no stray .env
    for name in ("TRACELOOM_ENV", "TRACELOOM_SECRET_KEY", "TRACELOOM_MODE", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_load():
    s = Settings()
    assert s.mode == "lite"
    assert s.signup_enabled is True
    assert s.rate_auth == "10/minute"
    assert s.rate_ingest == "600/minute"
    assert s.secret_key  # ephemeral in dev


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("TRACELOOM_SIGNUP_ENABLED", "false")
    monkeypatch.setenv("TRACELOOM_INGEST_MAX_EVENTS", "7")
    monkeypatch.setenv("SMTP_HOST", "mail.example")
    s = Settings()
    assert s.signup_enabled is False
    assert s.ingest_max_events == 7
    assert s.smtp_host == "mail.example"


def test_missing_secret_fails_outside_dev(monkeypatch):
    monkeypatch.setenv("TRACELOOM_ENV", "production")
    with pytest.raises(ValidationError, match="TRACELOOM_SECRET_KEY"):
        Settings()
    monkeypatch.setenv("TRACELOOM_SECRET_KEY", "x" * 32)
    assert Settings().secure_cookies is True
