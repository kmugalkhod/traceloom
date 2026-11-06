"""Journey: a <script> or <img onerror> payload in every prompt field is literal text in the
prompts list, editor, detail tabs, compare, preview and metrics. A dialog (alert) fails the test."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import Observation, Project, Trace, User
from traceloom.prompts import service
from traceloom.prompts.service import Actor

pytestmark = pytest.mark.e2e

SCRIPT = "<script>alert(1)</script>"
IMG = "<img src=x onerror=alert(1)>"
PAYLOAD = f"{SCRIPT} {IMG}"


def seed(live) -> None:
    with live.container.session_factory() as db:
        owner = db.scalar(select(User).where(User.email == live.account.email))
        project = db.get(Project, live.account.project_id)
        who = Actor.for_user(owner, Role.OWNER)
        for n in (1, 2):
            service.create_version(
                db,
                project,
                who,
                name="xss/probe",
                type="text",
                content=f"v{n} {PAYLOAD} {{{{who}}}}",
                config={"note": PAYLOAD},
                commit_message=f"commit {PAYLOAD}",
                tags=[SCRIPT],
                labels=["production"] if n == 1 else [],
            )
        service.create_version(
            db,
            project,
            who,
            name="xss/chat",
            type="chat",
            content=[{"role": "user", "content": PAYLOAD}],
            commit_message="chat",
        )
        start = datetime.now(UTC) - timedelta(minutes=2)
        db.add(Trace(project_id=project.id, id="trace-x", timestamp=start))
        db.add(
            Observation(
                project_id=project.id,
                id="obs-x",
                trace_id="trace-x",
                type="generation",
                start_time=start,
                end_time=start + timedelta(seconds=1),
                model=PAYLOAD,
                prompt_name="xss/probe",
                prompt_version=2,
                usage_details={"total": 3},
            )
        )
        db.commit()


def assert_text_only(page, session, locator: str) -> None:
    expect(page.locator(locator).first).to_contain_text("alert(1)")
    assert page.locator(f"{locator} script, {locator} img").count() == 0
    page.wait_for_timeout(250)  # a payload that was going to fire would have by now
    assert [p for p in session.problems if p.startswith("dialog")] == []


def test_payload_is_text_on_every_prompt_page(live, session):
    seed(live)
    page = session.page
    root = f"{live.url}/project/{live.account.project_id}/prompts"

    page.goto(f"{root}?q=alert")
    assert_text_only(page, session, "main")
    page.goto(f"{root}?tag={SCRIPT}")
    assert_text_only(page, session, "main")

    detail = f"{root}/xss/probe"
    for suffix in (
        "",
        "?version=1",
        "?tab=compare&from=1&to=2",
        f"?tab=preview&version=2&v_who={SCRIPT}",
        "?tab=metrics&version=2",
    ):
        page.goto(detail + suffix)
        if "metrics" in suffix:
            expect(page.locator("#metrics-pane")).to_contain_text("Linked generations")
        assert_text_only(page, session, "main")

    page.goto(f"{root}/xss/chat?tab=compare&from=1&to=1")
    assert page.locator("main script, main img").count() == 0

    page.goto(f"{root}/new?from=xss/probe")
    expect(page.locator("textarea[name=text]")).to_have_value(f"v2 {PAYLOAD} {{{{who}}}}")
    assert page.locator("main script, main img").count() == 0

    # Typing the payload into the editor highlights it as text and never runs it.
    page.fill("textarea[name=text]", f"{PAYLOAD} {{{{x}}}} @@@prompt:a|label=b@@@")
    hl = page.locator("[data-type-section=text] .ed-hl")
    expect(hl).to_contain_text("alert(1)")
    assert page.locator(".ed-hl script, .ed-hl img").count() == 0
    expect(hl.locator(".inc")).to_have_text("@@@prompt:a|label=b@@@")
    expect(page.locator("[data-vars] .var")).to_have_text(["{{x}}"])

    # A rejected save echoes the payload back as text.
    page.goto(f"{root}/new")
    page.fill("#pn", f"bad name {SCRIPT}")
    page.fill("textarea[name=text]", PAYLOAD)
    page.fill("#cm", "x")
    page.click("form[data-prompt-form] button[type=submit]")
    expect(page.locator("#e-pn")).to_be_visible()
    assert page.locator("main script, main img").count() == 0

    page.wait_for_timeout(300)
    assert [p for p in session.problems if p.startswith(("dialog", "pageerror"))] == []
