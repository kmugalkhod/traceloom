"""Journey 1 (AC-1): a new visitor signs up, creates an org, a project and a key in the browser,
runs `examples/agent.py` with those keys, and debugs the trace it sent.

This is the one test that sends real SDK output (the example agent with its offline fake model,
in a subprocess) to the real server over HTTP.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
EMAIL = "first.visitor@example.com"
PASSWORD = "a long first-trace passphrase"


def run_agent(url: str, public_key: str, secret_key: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TRACELOOM_", "ANTHROPIC_"))}
    env.update(TRACELOOM_HOST=url, TRACELOOM_PUBLIC_KEY=public_key, TRACELOOM_SECRET_KEY=secret_key)
    return subprocess.run(
        [sys.executable, str(ROOT / "examples" / "agent.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_first_trace_from_the_example_agent(live, e2e_browser):
    context = e2e_browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5000)
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("dialog", lambda d: (problems.append(f"dialog: {d.message}"), d.dismiss()))
    try:
        # Sign up; a new account has no organization yet, so the app asks for one.
        page.goto(f"{live.url}/signup")
        page.fill("#name", "First Visitor")
        page.fill("#email", EMAIL)
        page.fill("#password", PASSWORD)
        page.click('form[action="/signup"] button[type=submit]')
        expect(page).to_have_url(re.compile(r"/orgs/new$"))

        # The organization form creates the first project too, then opens its keys page.
        page.fill("#name", "First Org")
        page.fill("#project", "first-project")
        page.click('form[action="/orgs/new"] button[type=submit]')
        expect(page).to_have_url(re.compile(r"/project/[^/]+/settings/keys$"))
        project_id = page.url.split("/project/")[1].split("/")[0]

        # Create a key and read the pair off the page (the secret is shown only once).
        page.click("button:has-text('Create an API key')")
        public_key = page.locator("#new-public").inner_text().strip()
        secret_key = page.locator("#new-secret").inner_text().strip()
        assert public_key.startswith("tl-pk-") and secret_key.startswith("tl-sk-")

        # Run the example agent against the live server with those keys.
        result = run_agent(live.url, public_key, secret_key)
        assert result.returncode == 0, result.stderr
        assert "trace:" in result.stderr, result.stdout + result.stderr  # delivered
        trace_id = result.stderr.split("trace:")[1].split()[0]

        # The trace is in the list within 5 s of the agent finishing.
        list_url = f"{live.url}/project/{project_id}/traces"
        row = page.locator(f"a.row-link[href$='/traces/{trace_id}']")
        deadline = time.monotonic() + 5
        page.goto(list_url)
        while row.count() == 0 and time.monotonic() < deadline:
            page.reload()
        expect(row).to_be_visible()
        row.click()
        expect(page).to_have_url(re.compile(rf"/traces/{re.escape(trace_id)}"))

        # Tree: at least three levels, with the failing tool highlighted.
        tree = page.locator("[data-body=tree]")
        expect(tree).to_be_visible()
        levels = tree.locator("[role=treeitem]").evaluate_all(
            "els => els.map(e => Number(e.getAttribute('aria-level')))"
        )
        assert max(levels) >= 3, levels
        expect(tree.locator("[role=treeitem].ERROR")).to_contain_text("lookup_order")

        # Timeline: Gantt bars and a time-to-first-token marker.
        page.keyboard.press("t")
        timeline = page.locator("[data-body=timeline]")
        expect(timeline).to_be_visible()
        expect(timeline.locator(".tl-svg line.t").first).to_be_attached()
        page.keyboard.press("t")
        expect(tree).to_be_visible()

        # Cost: the trace total is above zero, and a generation's cost is calculated by us.
        cost = page.locator(".kv div:has(dt:text-is('Cost')) dd")
        expect(cost).to_have_text(re.compile(r"\$\d"))
        assert float(cost.inner_text().strip().lstrip("$").replace(",", "")) > 0
        expect(page.locator(".kv div:has(dt:text-is('Tokens')) dd")).to_have_text(
            re.compile(r"[1-9]")
        )
        tree.locator("[role=treeitem] a:has-text('planner')").first.click()
        pane = page.locator("#io-pane")
        expect(pane.locator("h2")).to_have_text("planner")
        expect(pane.locator(".src.calc")).to_have_text("calculated")

        # `e` jumps to the first error and selects the failing tool.
        page.keyboard.press("e")
        expect(tree.locator("[role=treeitem][aria-selected=true]")).to_contain_text("lookup_order")
        expect(pane.locator("h2")).to_have_text("lookup_order")
        assert problems == []
    finally:
        context.close()
