"""Browser journeys: the real app on a real port, driven by Playwright.

Chromium is used when installed, otherwise the system Chrome channel. When neither can start,
the e2e tests skip (with the reason) instead of failing, so the default run works anywhere.
"""

from __future__ import annotations

import base64
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn

from traceloom.auth.api_keys import generate_key_pair, hash_secret
from traceloom.auth.passwords import hash_password
from traceloom.config import Settings
from traceloom.db.models import ApiKey, Membership, Organization, Project, User
from traceloom.main import create_app

PASSWORD = "correct horse battery"


@dataclass
class Account:
    email: str
    project_id: str
    public_key: str
    secret_key: str


@dataclass
class Live:
    url: str
    app: object
    account: Account

    @property
    def container(self):
        return self.app.state.container

    def ingest(self, events: list[dict]) -> None:
        auth = base64.b64encode(
            f"{self.account.public_key}:{self.account.secret_key}".encode()
        ).decode()
        r = httpx.post(
            f"{self.url}/api/public/ingestion",
            json={"batch": events},
            headers={"Authorization": f"Basic {auth}"},
            timeout=10,
        )
        assert r.status_code == 207 and not r.json()["errors"], r.text
        deadline = time.time() + 10
        while self.container.queue.pending and time.time() < deadline:
            time.sleep(0.05)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(tmp_path: Path) -> Iterator[Live]:
    settings = Settings(
        TRACELOOM_ENV="test",
        DATABASE_URL=f"sqlite:///{tmp_path / 'e2e.db'}",
        TRACELOOM_BLOB_DIR=str(tmp_path / "blobs"),
        TRACELOOM_SERVER_SECRET="e2e-secret-not-for-production",
    )
    app = create_app(settings)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "the live server did not start"
    container = app.state.container
    public, secret = generate_key_pair()
    with container.session_factory.begin() as s:
        user = User(
            email="maria@example.com", name="Maria Chen", password_hash=hash_password(PASSWORD)
        )
        org = Organization(name="Acme Labs")
        s.add_all([user, org])
        s.flush()
        project = Project(org_id=org.id, name="support-agent")
        s.add_all([Membership(user_id=user.id, org_id=org.id, role="owner"), project])
        s.flush()
        s.add(
            ApiKey(
                project_id=project.id,
                public_key=public,
                secret_hash=hash_secret(secret, settings.secret_key),
                note="e2e",
            )
        )
        project_id = project.id
    yield Live(
        f"http://127.0.0.1:{port}", app, Account("maria@example.com", project_id, public, secret)
    )
    server.should_exit = True
    thread.join(timeout=10)


# Module scope, not session: the sync Playwright API keeps an asyncio loop running in this
# thread while it is open, which breaks later tests that call asyncio.run() (the SDK suite).
@pytest.fixture(scope="module")
def e2e_browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        errors = []
        for kwargs in ({}, {"channel": "chrome"}):
            try:
                b = p.chromium.launch(**kwargs)
                break
            except Exception as exc:  # no Chromium download, no system Chrome
                errors.append(str(exc).splitlines()[0])
        else:
            pytest.skip("no browser available for e2e: " + "; ".join(errors))
        yield b
        b.close()


@dataclass
class Session:
    page: object
    problems: list[str]


@pytest.fixture
def session(e2e_browser, live) -> Iterator[Session]:
    """A signed-in browser page. `problems` collects console errors and any dialog (alert)."""
    context = e2e_browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5000)
    problems: list[str] = []
    page.on(
        "console", lambda m: problems.append(f"console: {m.text}") if m.type == "error" else None
    )
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

    def on_dialog(dialog):
        problems.append(f"dialog: {dialog.message}")
        dialog.dismiss()

    page.on("dialog", on_dialog)
    page.goto(f"{live.url}/login")
    page.fill("#email", live.account.email)
    page.fill("#password", PASSWORD)
    page.click('form[action="/login"] button[type=submit]')
    page.wait_for_load_state()
    yield Session(page, problems)
    context.close()


def trace_events(
    i: int,
    *,
    name="support-agent",
    output="",
    input_="",
    region="eu",
    minutes_ago=5,
    tags=("support",),
    user="usr_maria",
    level="DEFAULT",
) -> list[dict]:
    """The ingestion events for one trace with a generation and a tool step."""
    now = datetime.now(UTC) - timedelta(minutes=minutes_ago)

    def stamp(seconds: float) -> str:
        return (now + timedelta(seconds=seconds)).isoformat()

    tid = f"trace-{i:03}"
    return [
        {
            "id": f"{tid}-t",
            "type": "trace-create",
            "timestamp": stamp(0),
            "body": {
                "id": tid,
                "name": name,
                "userId": user,
                "tags": list(tags),
                "metadata": {"region": region},
                "input": input_ or f"question {i}",
                "output": output or f"answer {i}",
                "timestamp": stamp(0),
            },
        },
        {
            "id": f"{tid}-root",
            "type": "span-create",
            "timestamp": stamp(0),
            "body": {
                "id": f"{tid}-root",
                "traceId": tid,
                "name": name,
                "type": "agent",
                "startTime": stamp(0),
                "endTime": stamp(4),
            },
        },
        {
            "id": f"{tid}-gen",
            "type": "generation-create",
            "timestamp": stamp(1),
            "body": {
                "id": f"{tid}-gen",
                "traceId": tid,
                "parentObservationId": f"{tid}-root",
                "name": "planner",
                "model": "claude-sonnet-4-5",
                "startTime": stamp(0.1),
                "completionStartTime": stamp(0.5),
                "endTime": stamp(2),
                "input": [{"role": "user", "content": input_ or f"question {i}"}],
                "output": output or f"answer {i}",
                "usageDetails": {"input": 900, "output": 120},
            },
        },
        {
            "id": f"{tid}-tool",
            "type": "span-create",
            "timestamp": stamp(2),
            "body": {
                "id": f"{tid}-tool",
                "traceId": tid,
                "parentObservationId": f"{tid}-root",
                "name": "lookup_order",
                "type": "tool",
                "startTime": stamp(2),
                "endTime": stamp(3),
                "level": level,
                "statusMessage": "order service timed out" if level == "ERROR" else None,
            },
        },
    ]


@pytest.fixture
def events() -> Callable[..., list[dict]]:
    return trace_events
