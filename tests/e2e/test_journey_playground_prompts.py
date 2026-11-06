"""Journey: Save as prompt (new and new version), Load prompt, Open in playground from a trace."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from tests.e2e.conftest import trace_events
from traceloom.auth.permissions import Role
from traceloom.db.models import Project, Prompt, PromptVersion, User
from traceloom.llm import connections, providers
from traceloom.llm.connections import ConnectionPolicy
from traceloom.llm.providers import BaseProvider, DoneDelta, TextDelta
from traceloom.prompts import service
from traceloom.prompts.service import Actor

pytestmark = pytest.mark.e2e

CHAT_V1 = [
    {"role": "system", "content": "You are terse."},
    {"role": "user", "content": "First {{q}}"},
]
CHAT_V2 = [
    {"role": "system", "content": "You are warm."},
    {"role": "user", "content": "Second {{q}}"},
]


class Quiet(BaseProvider):
    def _stream(self, *args):
        yield TextDelta("ok")
        yield DoneDelta("stop")


@pytest.fixture
def setup(session, live):
    providers.register("anthropic", Quiet)
    with live.container.session_factory() as db:
        owner = db.scalar(select(User).where(User.email == live.account.email))
        project = db.get(Project, live.account.project_id)
        actor = Actor.for_user(owner, Role.OWNER)
        connections.create(
            db, project, actor, ConnectionPolicy.from_settings(live.container.settings),
            provider="anthropic", name="Claude fake", secrets={"api_key": "sk-ant-fake-123456"},
        )  # fmt: skip
        for content, note in ((CHAT_V1, "v1"), (CHAT_V2, "v2")):
            service.create_version(
                db,
                project,
                actor,
                name="support",
                type="chat",
                content=content,
                commit_message=note,
            )
        db.commit()
    yield session
    providers.unregister("anthropic")


def playground(live, query=""):
    return f"{live.url}/project/{live.account.project_id}/playground{query}"


def menu(page, action):
    page.click("[data-menu-toggle=pg-menu]")
    page.click(f"[data-pg-action={action}]")


def versions(live, name):
    with live.container.session_factory() as db:
        prompt = db.scalars(select(Prompt).where(Prompt.name == name)).one()
        rows = db.scalars(
            select(PromptVersion)
            .where(PromptVersion.prompt_id == prompt.id)
            .order_by(PromptVersion.version)
        ).all()
        return [(v.version, v.commit_message, v.content, v.config) for v in rows]


def test_save_as_a_new_prompt_then_as_a_new_version(setup, live):
    page = setup.page
    page.goto(playground(live))
    page.locator("[data-msg-text]").last.fill("Greet {{who}}")
    menu(page, "save-as-prompt")
    dialog = page.locator("#save-prompt")
    expect(dialog).to_be_visible()
    dialog.locator("#sp-name").fill("pg/greeting")
    dialog.locator("#sp-msg").fill("first draft")
    dialog.get_by_role("button", name="Save prompt").click()
    expect(dialog.locator("[data-save-done]")).to_contain_text("Saved pg/greeting as version 1")
    assert versions(live, "pg/greeting")[0][:2] == (1, "first draft")

    # a taken name cannot be saved as "new"
    dialog.locator("#sp-msg").fill("again")
    dialog.get_by_role("button", name="Save prompt").click()
    expect(dialog.locator("[data-save-error]")).to_contain_text("already exists")

    dialog.get_by_label("New version of an existing prompt").check()
    dialog.locator("#sp-existing").select_option("pg/greeting")
    dialog.locator("#sp-msg").fill("second draft")
    dialog.get_by_role("button", name="Save prompt").click()
    expect(dialog.locator("[data-save-done]")).to_contain_text("as version 2")
    rows = versions(live, "pg/greeting")
    assert [(n, msg) for n, msg, *_ in rows] == [(1, "first draft"), (2, "second draft")]
    assert rows[1][2][-1]["content"] == "Greet {{who}}"
    link = dialog.locator("[data-save-done] a")
    assert link.get_attribute("href").endswith("/prompts/pg/greeting?version=2")
    # the one console error is the browser noting the refused "new" save above (HTTP 400)
    assert [p for p in setup.problems if "400" not in p] == []


def test_load_prompt_opens_the_chosen_version(setup, live):
    page = setup.page
    page.goto(playground(live))
    menu(page, "load-prompt")
    dialog = page.locator("#load-prompt")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#lp-version")).to_have_value("2")  # the latest
    dialog.locator("#lp-version").fill("1")
    dialog.get_by_role("button", name="Load").click()
    page.wait_for_url("**/playground?prompt=support&version=1")
    expect(page.locator("[data-msg-text]").nth(1)).to_have_value("First {{q}}")
    assert setup.problems == []


def test_open_in_playground_from_a_generation(setup, live):
    live.ingest(trace_events(1, input_="question 1"))
    page = setup.page
    pid = live.account.project_id
    page.goto(f"{live.url}/project/{pid}/traces/trace-001?obs=trace-001-gen")
    button = page.locator("#io-pane [data-open-playground]")
    expect(button).to_have_text("Open in playground")
    button.click()
    page.wait_for_url("**/playground?trace=trace-001&obs=trace-001-gen")
    expect(page.locator("#pg-model")).to_have_value("claude-sonnet-4-5")
    expect(page.locator("[data-msg-text]").first).to_have_value("question 1")
    # the row action in the tree opens the same place
    page.goto(f"{live.url}/project/{pid}/traces/trace-001")
    page.locator(".node", has_text="planner").hover()
    page.locator(".node .row-act").click()
    page.wait_for_url("**/playground?trace=trace-001&obs=trace-001-gen")
    assert setup.problems == []


@pytest.mark.parametrize("width", [390, 360, 768])
def test_open_in_playground_stays_inside_its_card_on_phones(setup, live, width):
    events = trace_events(1, input_="question 1")
    for e in events:  # a prompt-linked generation has the longest header
        if e["id"] == "trace-001-gen":
            e["body"].update(promptName="support/customer-greeting-long-name", promptVersion=12)
    live.ingest(events)
    page = setup.page
    page.set_viewport_size({"width": width, "height": 800})
    pid = live.account.project_id
    page.goto(f"{live.url}/project/{pid}/traces/trace-001?obs=trace-001-gen")
    button = page.locator("#io-pane [data-open-playground]")
    expect(button).to_be_visible()
    name = button.get_attribute("aria-label") or button.inner_text()
    assert "playground" in name.lower()  # still named when the text is shortened to an icon
    card = page.locator("#io-pane .io-sec").first.bounding_box()
    box = button.bounding_box()
    assert box["x"] >= card["x"] - 1 and box["x"] + box["width"] <= card["x"] + card["width"] + 1
    scroll = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert scroll <= 0  # no horizontal page scroll
