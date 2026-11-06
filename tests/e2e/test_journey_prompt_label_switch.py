"""Journey (AC-1, AC-2, AC-3): an app fetches `support` from production, an Admin moves the label
in the browser, and the running app gets the new version within its cache TTL, linked and counted.

`examples/prompt_app.py` runs as a real subprocess against the live server with a 1 s cache TTL.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import Project, User
from traceloom.ports import PromptMetricsQuery
from traceloom.prompts import service
from traceloom.prompts.service import Actor

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
CHAT = [
    {"role": "system", "content": "You are a support assistant. Version {version}."},
    {"role": "user", "content": "Hello {{name}}: {{question}}"},
]


def seed_support(live) -> None:
    """support v1..v3 (chat), production on v2."""
    with live.container.session_factory() as db:
        owner = db.scalar(select(User).where(User.email == live.account.email))
        project = db.get(Project, live.account.project_id)
        who = Actor.for_user(owner, Role.OWNER)
        for n in (1, 2, 3):
            messages = [dict(m) for m in CHAT]
            messages[0]["content"] = messages[0]["content"].format(version=n)
            service.create_version(
                db,
                project,
                who,
                name="support",
                type="chat",
                content=messages,
                commit_message=f"version {n}",
            )
        service.move_label(db, project, who, "support", "production", 2)
        db.commit()


def start_app(live, **env) -> tuple[subprocess.Popen, queue.Queue[str]]:
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "examples" / "prompt_app.py"),
            "--repeat",
            "40",
            "--interval",
            "0.4",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "TRACELOOM_HOST": live.url,
            "TRACELOOM_PUBLIC_KEY": live.account.public_key,
            "TRACELOOM_SECRET_KEY": live.account.secret_key,
            "TRACELOOM_PROMPT_CACHE_TTL": "1",
            "TRACELOOM_FLUSH_INTERVAL": "0.2",
            **env,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        for line in process.stdout:  # type: ignore[union-attr]
            lines.put(line.rstrip())

    threading.Thread(target=pump, daemon=True).start()
    return process, lines


def next_version(lines: queue.Queue[str], timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        if line.startswith("prompt:"):
            return line
    raise AssertionError("the app printed no prompt line in time")


def test_label_move_reaches_the_running_app_within_the_ttl(live, session):
    seed_support(live)
    page = session.page
    base = f"{live.url}/project/{live.account.project_id}/prompts/support"
    process, lines = start_app(live)
    try:
        # AC-1: production is v2, the chat prompt is compiled with the variables substituted.
        assert next_version(lines) == "prompt: support v2"

        # Move production to v3 in the browser (one click, no confirm).
        page.goto(f"{base}?version=3")
        page.get_by_role("button", name="Set production").click()
        expect(page.locator("#move-toast")).to_contain_text("now points at v3")

        # AC-2: the app picks up v3 on its own, with no restart, once the 1 s TTL has passed.
        seen = []
        deadline = time.time() + 12
        while time.time() < deadline:
            seen.append(next_version(lines))
            if seen[-1] == "prompt: support v3":
                break
        assert seen[-1] == "prompt: support v3", seen
        assert all(s in ("prompt: support v2", "prompt: support v3") for s in seen)
    finally:
        process.terminate()
        process.wait(timeout=10)

    # AC-3: generations are stored linked, and the metrics tab counts them.
    deadline = time.time() + 10
    store = live.container.trace_store
    while time.time() < deadline:
        if store.prompt_metrics(live.account.project_id, "support", 2, PromptMetricsQuery()).count:
            break
        time.sleep(0.2)
    v2 = store.prompt_metrics(live.account.project_id, "support", 2, PromptMetricsQuery())
    assert v2.count >= 1

    page.goto(f"{base}?tab=metrics&version=2&range=24h")
    pane = page.locator("#metrics-pane")
    expect(pane).to_contain_text("Linked generations")
    expect(pane.locator(".stat").first).to_contain_text(str(v2.count))
    expect(pane.locator("tbody tr").first).to_be_visible()
    assert [p for p in session.problems if p.startswith(("dialog", "pageerror"))] == []


def test_fallback_run_is_not_linked_and_not_counted(live, session):
    """With the prompt unreachable the app runs on its fallback and links nothing."""
    seed_support(live)
    process = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "prompt_app.py")],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "TRACELOOM_HOST": "http://127.0.0.1:9",  # nothing listens here
            "TRACELOOM_PUBLIC_KEY": live.account.public_key,
            "TRACELOOM_SECRET_KEY": live.account.secret_key,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert process.returncode == 0, process.stderr
    assert "prompt: fallback" in process.stdout
    metrics = live.container.trace_store.prompt_metrics(
        live.account.project_id, "support", None, PromptMetricsQuery()
    )
    assert metrics.count == 0
