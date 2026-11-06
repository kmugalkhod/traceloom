"""A real server on a free port plus a Playwright page, for the browser journeys.

Import the fixtures into a test module (`from tests.e2e.live_server import *`). The tests skip
when Chromium cannot start, so the default run stays green on machines without browsers.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
import uvicorn

from traceloom.config import Settings
from traceloom.main import create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Live:
    url: str
    app: object

    @property
    def container(self):
        return self.app.state.container


@pytest.fixture
def live_server(tmp_path) -> Iterator[Live]:
    settings = Settings(
        TRACELOOM_ENV="test",
        TRACELOOM_MODE="lite",
        DATABASE_URL=f"sqlite:///{tmp_path / 'e2e.db'}",
        TRACELOOM_BLOB_DIR=str(tmp_path / "blobs"),
        TRACELOOM_SERVER_SECRET="e2e-server-secret-not-for-production",
    )
    app = create_app(settings)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "live server did not start"
    yield Live(f"http://127.0.0.1:{port}", app)
    server.should_exit = True
    thread.join(timeout=10)


def _launch(p):
    """Playwright's own Chromium, else installed Chrome, else any cached Chromium build."""
    import glob
    import os

    attempts = [{}, {"channel": "chrome"}]
    cache = os.path.expanduser("~/Library/Caches/ms-playwright")
    cache_alt = os.path.expanduser("~/.cache/ms-playwright")
    for root in (cache, cache_alt):
        for pattern in (
            "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
            "chromium-*/chrome-*/Chromium.app/Contents/MacOS/Chromium",
            "chromium-*/chrome-linux*/chrome",
        ):
            for path in sorted(glob.glob(os.path.join(root, pattern)), reverse=True):
                attempts.append({"executable_path": path})
    last = None
    for kwargs in attempts:
        try:
            return p.chromium.launch(**kwargs)
        except Exception as exc:
            last = exc
    pytest.skip(f"Chromium is not available: {last}")


@pytest.fixture
def browser_page() -> Iterator:
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as p:
        browser = _launch(p)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        problems: list[str] = []
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
        page.on("dialog", lambda d: (problems.append(f"dialog: {d.message}"), d.dismiss()))
        page.problems = problems  # type: ignore[attr-defined]
        yield page
        browser.close()


def sign_in(page, live: Live, user_email: str, password: str) -> None:
    page.goto(live.url + "/login")
    page.fill('input[name="email"]', user_email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def make_world(live: Live, roles=("viewer", "member", "admin")) -> dict:
    """An org, one project and a user per role (password: tests.factories.DEFAULT_PASSWORD)."""
    from tests.factories import default_password_hash
    from traceloom.db.models import Membership, Organization, Project, User

    out: dict = {}
    with live.container.session_factory() as db:
        org = Organization(name="E2E Org")
        db.add(org)
        db.flush()
        project = Project(org_id=org.id, name="E2E Project")
        db.add(project)
        for role in roles:
            user = User(
                email=f"{role}@example.com", name=f"{role.title()} Person",
                password_hash=default_password_hash(),
            )  # fmt: skip
            db.add(user)
            db.flush()
            db.add(Membership(user_id=user.id, org_id=org.id, role=role))
            out[role] = user.email
        db.commit()
        out["org_id"], out["project_id"] = org.id, project.id
    return out
