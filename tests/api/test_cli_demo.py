from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select

from traceloom import cli
from traceloom.config import Settings
from traceloom.db.models import ApiKey, Membership, Organization, Project, User
from traceloom.db.session import make_engine, make_session_factory


@pytest.fixture
def cli_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.delenv("TRACELOOM_SECRET_KEY", raising=False)  # the SDK variable, not the server's
    monkeypatch.setenv("TRACELOOM_SERVER_SECRET", settings.secret_key)
    monkeypatch.setenv("TRACELOOM_BLOB_DIR", settings.blob_dir)
    monkeypatch.setenv("TRACELOOM_MODE", "lite")
    monkeypatch.setenv("TRACELOOM_ENV", "test")
    cli.get_settings.cache_clear()
    yield settings
    cli.get_settings.cache_clear()


def test_migrate_brings_an_empty_database_to_head(cli_settings, capsys):
    assert cli.main(["migrate"]) == 0
    engine = make_engine(cli_settings.database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"users", "projects", "api_keys", "traces", "observations", "scores"} <= tables
    assert cli.main(["migrate"]) == 0  # running again is a no-op


def counts(settings: Settings) -> tuple[int, ...]:
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as db:
            return tuple(
                db.scalar(select(func.count()).select_from(m))
                for m in (User, Organization, Membership, Project, ApiKey)
            )
    finally:
        engine.dispose()


def test_seed_demo_is_idempotent_and_prints_credentials(cli_settings, capsys):
    assert cli.main(["seed-demo"]) == 0
    first = capsys.readouterr().out
    assert cli.main(["seed-demo"]) == 0
    second = capsys.readouterr().out
    assert counts(cli_settings) == (1, 1, 1, 1, 1)
    for label in ("email:", "password:", "project id:", "public key:", "secret key:"):
        assert label in first
    values = lambda text: {  # noqa: E731
        ln.split(":", 1)[0]: ln.split(":", 1)[1].strip() for ln in text.splitlines() if ": " in ln
    }
    a, b = values(first), values(second)
    for key in ("email", "password", "project id", "public key", "secret key"):
        assert a[key] == b[key]
    assert "created" in first and "already present" in second
    assert a["public key"].startswith("tl-pk-") and a["secret key"].startswith("tl-sk-")


def test_seeded_key_authenticates(cli_settings, capsys):
    from traceloom.auth.api_keys import hash_secret

    cli.main(["seed-demo"])
    out = capsys.readouterr().out
    secret = next(
        ln.split(":", 1)[1].strip() for ln in out.splitlines() if ln.startswith("secret key")
    )
    engine = make_engine(cli_settings.database_url)
    try:
        with make_session_factory(engine)() as db:
            key = db.scalar(select(ApiKey))
            assert key.secret_hash == hash_secret(secret, cli_settings.secret_key)
    finally:
        engine.dispose()


def test_worker_starts_and_stops_cleanly(cli_settings, capsys, tmp_path: Path):
    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(cli.run_worker(cli_settings, stop)))
    thread.start()
    deadline = time.time() + 20
    while "worker started" not in capsys.readouterr().out and time.time() < deadline:
        if not thread.is_alive():
            break
        time.sleep(0.05)
    assert thread.is_alive()
    stop.set()
    thread.join(timeout=20)
    assert not thread.is_alive() and result == [0]


def test_seed_demo_needs_only_the_server_secret_and_its_keys_ingest(
    cli_settings, capsys, monkeypatch
):
    """With TRACELOOM_SERVER_SECRET as the only secret in the environment, the command is
    idempotent and the keys it prints are accepted by the ingestion API."""
    import base64
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from traceloom.main import create_app

    assert "TRACELOOM_SECRET_KEY" not in os.environ
    assert cli.main(["seed-demo"]) == 0
    first = capsys.readouterr().out
    assert cli.main(["seed-demo"]) == 0
    second = capsys.readouterr().out
    assert counts(cli_settings) == (1, 1, 1, 1, 1)

    def creds(text):
        return {
            ln.split(":", 1)[0]: ln.split(":", 1)[1].strip()
            for ln in text.splitlines()
            if ": " in ln
        }

    a, b = creds(first), creds(second)
    assert a["public key"] == b["public key"] and a["secret key"] == b["secret key"]

    auth = base64.b64encode(f"{a['public key']}:{a['secret key']}".encode()).decode()
    batch = {
        "batch": [
            {
                "id": "e1",
                "type": "trace-create",
                "timestamp": datetime.now(UTC).isoformat(),
                "body": {"id": "demo-trace", "name": "demo"},
            }
        ]
    }
    with TestClient(create_app(cli.get_settings())) as client:
        ok = client.post(
            "/api/public/ingestion", json=batch, headers={"Authorization": f"Basic {auth}"}
        )
        assert ok.status_code == 207 and ok.json()["successes"] == [{"id": "e1", "status": 201}]
        bad = client.post(
            "/api/public/ingestion",
            json=batch,
            headers={
                "Authorization": "Basic "
                + base64.b64encode(f"{a['public key']}:tl-sk-wrong".encode()).decode()
            },
        )
        assert bad.status_code == 401


def test_seed_demo_login_works_with_the_printed_credentials(cli_settings, capsys):
    """The printed email must be one the login form accepts (no reserved domains)."""
    from fastapi.testclient import TestClient

    from traceloom.main import create_app

    assert cli.main(["seed-demo"]) == 0
    out = capsys.readouterr().out
    creds = {
        ln.split(":", 1)[0]: ln.split(":", 1)[1].strip() for ln in out.splitlines() if ": " in ln
    }
    with TestClient(create_app(cli.get_settings())) as client:
        client.get("/login")
        client.headers["X-CSRF-Token"] = client.cookies["tl_csrf"]
        r = client.post(
            "/login",
            data={"email": creds["email"], "password": creds["password"]},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text[:300]
        assert client.get("/", follow_redirects=False).headers["location"] != "/login"


def test_seed_demo_credentials_come_from_settings(cli_settings, capsys, monkeypatch):
    monkeypatch.setenv("TRACELOOM_DEMO_EMAIL", "owner@example.org")
    monkeypatch.setenv("TRACELOOM_DEMO_PASSWORD", "another-demo-pass-456")
    cli.get_settings.cache_clear()
    assert cli.get_settings().demo_email == "owner@example.org"
    assert cli.get_settings().demo_password == "another-demo-pass-456"
    assert cli.main(["seed-demo"]) == 0
    out = capsys.readouterr().out
    assert "owner@example.org" in out and "another-demo-pass-456" in out
