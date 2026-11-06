"""Journey (AC-10): on the playground page, three variants with different models stream side by
side, each with its own usage and cost; a fifth cannot be added; the columns stack under 1100px."""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import Project, User
from traceloom.llm import connections, providers
from traceloom.llm.connections import ConnectionPolicy
from traceloom.llm.providers import BaseProvider, DoneDelta, TextDelta, UsageDelta
from traceloom.prompts.service import Actor

pytestmark = pytest.mark.e2e

MODELS = ["claude-haiku-4-5-20251001", "claude-sonnet-5-5", "claude-opus-5-5"]
STARTS: list[float] = []
CALLS: list[list[dict]] = []
CLOSED: list[str] = []  # every provider stream that was closed (finished or dropped)


class PerModel(BaseProvider):
    """Streams the model name, slowly, with usage that depends on the model."""

    def _stream(self, messages, model, params, tools, schema):
        CALLS.append(list(messages))
        if model == "slow":
            try:
                time.sleep(0.5)  # "Waiting": nothing has streamed yet
                for i in range(200):
                    yield TextDelta(f"w{i} ")
                    time.sleep(0.05)
            finally:
                CLOSED.append(model)
            return
        if model == "boom":
            yield TextDelta("partial ")
            raise RuntimeError(f"upstream refused {self.cfg.api_key}")
        STARTS.append(time.monotonic())
        for word in (model, "says", "hello"):
            yield TextDelta(word + " ")
            time.sleep(0.25)
        size = 100 * (MODELS.index(model) + 1)
        yield UsageDelta(size, size * 2)
        yield DoneDelta("stop")


KEY = "sk-ant-fake-123456"


@pytest.fixture
def pg(session, live):
    providers.register("anthropic", PerModel)
    STARTS.clear()
    CALLS.clear()
    CLOSED.clear()
    with live.container.session_factory() as db:
        owner = db.scalar(select(User).where(User.email == live.account.email))
        project = db.get(Project, live.account.project_id)
        connections.create(
            db, project, Actor.for_user(owner, Role.OWNER),
            ConnectionPolicy.from_settings(live.container.settings),
            provider="anthropic", name="Claude fake", secrets={"api_key": KEY},
        )  # fmt: skip
        db.commit()
    page = session.page
    page.goto(f"{live.url}/project/{live.account.project_id}/playground")
    page.locator("[data-msg-text]").last.fill("Say hello")
    page.fill("#pg-model", MODELS[0])
    yield session
    providers.unregister("anthropic")


def open_compare(page):
    page.click("[data-menu-toggle=pg-menu]")
    page.click("[data-pg-action=compare]")
    expect(page.locator("#pg-root")).to_have_attribute("data-mode", "compare")
    expect(page.locator("#pg-msgs")).to_be_visible()  # the shared messages stay on screen


def test_three_variants_stream_independently(pg):
    page = pg.page
    open_compare(page)
    expect(page.locator("[data-variant]")).to_have_count(2)
    page.click("[data-add-variant]")
    columns = page.locator("[data-variant]")
    expect(columns).to_have_count(3)
    for i, model in enumerate(MODELS):
        columns.nth(i).locator("[data-v-model]").fill(model)
    page.click("[data-run-all]")
    for i, model in enumerate(MODELS):
        col = columns.nth(i)
        expect(col.locator("[data-v-text]")).to_contain_text(f"{model} says hello")
        size = 100 * (i + 1)
        expect(col.locator("[data-u-input]")).to_have_text(f"{size:,}")
        expect(col.locator("[data-u-output]")).to_have_text(f"{size * 2:,}")
        expect(col.locator("[data-u-cost]")).to_contain_text("$")
    assert len(STARTS) == 3 and max(STARTS) - min(STARTS) < 0.5  # they ran at the same time
    assert pg.problems == []


def test_a_fifth_variant_cannot_be_added(pg):
    page = pg.page
    open_compare(page)
    for _ in range(2):
        page.click("[data-add-variant]")
    expect(page.locator("[data-variant]")).to_have_count(4)
    add = page.locator("[data-add-variant]")
    expect(add).to_be_disabled()
    page.evaluate("document.querySelector('[data-add-variant]').click()")
    expect(page.locator("[data-variant]")).to_have_count(4)
    page.locator("[data-variant-remove]").first.click()
    expect(add).to_be_enabled()


def test_columns_sit_side_by_side_wide_and_stack_under_1100px(pg):
    page = pg.page
    open_compare(page)
    page.click("[data-add-variant]")
    page.click("[data-add-variant]")
    boxes = lambda: [v.bounding_box() for v in page.locator("[data-variant]").all()]  # noqa: E731
    wide = boxes()
    assert wide[0]["y"] == wide[1]["y"] and wide[0]["x"] < wide[1]["x"]  # 2x2 on a wide screen
    assert wide[2]["y"] > wide[0]["y"] and wide[2]["x"] == wide[0]["x"]
    page.set_viewport_size({"width": 1000, "height": 900})
    narrow = boxes()
    assert all(b["x"] == narrow[0]["x"] for b in narrow)  # one column
    assert all(narrow[i + 1]["y"] > narrow[i]["y"] for i in range(3))


def test_errors_are_inline_scrubbed_and_per_column(pg):
    page = pg.page
    open_compare(page)
    columns = page.locator("[data-variant]")
    columns.nth(0).locator("[data-v-model]").fill(MODELS[1])
    columns.nth(1).locator("[data-v-model]").fill("boom")
    page.click("[data-run-all]")
    expect(columns.nth(0).locator("[data-v-text]")).to_contain_text(f"{MODELS[1]} says hello")
    failed = columns.nth(1)
    expect(failed.locator("[data-v-error]")).to_contain_text("upstream refused")
    expect(failed.locator("[data-v-text]")).to_contain_text("partial")
    assert KEY not in page.content() and "123456" not in failed.inner_text()
    expect(columns.nth(0).locator("[data-v-error]")).to_be_hidden()


def test_a_column_with_its_own_messages_and_stop(pg):
    page = pg.page
    open_compare(page)
    col = page.locator("[data-variant]").nth(1)
    col.locator("[data-v-follow]").uncheck()
    expect(col.locator(".v-msg")).to_have_count(2)  # started as a copy of the shared messages
    expect(col.locator("[data-m-content]").last).to_have_value("Say hello")
    col.locator("[data-m-content]").last.fill("A different question")
    col.locator("[data-v-model]").fill(MODELS[2])
    page.locator("[data-variant]").nth(0).locator("[data-v-model]").fill(MODELS[0])
    page.click("[data-run-all]")
    expect(col.locator("[data-variant-stop]")).to_be_visible()
    col.locator("[data-variant-stop]").click()
    expect(col.locator("[data-variant-stop]")).to_be_hidden()
    expect(page.locator("[data-variant]").nth(0).locator("[data-u-input]")).to_have_text("100")
    expect(col.locator("[data-u-input]")).to_be_hidden()  # a stopped column reports no usage


def test_shared_messages_and_variables_stay_visible_and_drive_every_column(pg):
    page = pg.page
    open_compare(page)
    expect(page.locator("#pg-msgs")).to_be_visible()
    expect(page.locator("#pg-add")).to_be_visible()
    expect(page.locator("#pg-out")).to_be_hidden()  # the single response panel is replaced
    page.locator("[data-msg-text]").last.fill("Ask {{who}} about it")
    expect(page.locator("#pg-vars label")).to_have_text(
        ["who"]
    )  # variables appear and are editable
    page.locator("#pg-vars input").fill("Ada")
    columns = page.locator("[data-variant]")
    columns.nth(0).locator("[data-v-model]").fill(MODELS[0])
    columns.nth(1).locator("[data-v-model]").fill(MODELS[1])
    page.click("[data-run-all]")
    expect(columns.nth(1).locator("[data-u-input]")).to_have_text("200")
    assert len(CALLS) == 2 and all(c[-1]["content"] == "Ask Ada about it" for c in CALLS)
    # the shared message is still editable between runs, and the next run follows it
    page.locator("[data-msg-text]").last.fill("Second {{who}}")
    CALLS.clear()
    page.click("[data-run-all]")
    expect(columns.nth(1).locator("[data-u-input]")).to_have_text("200")
    assert all(c[-1]["content"] == "Second Ada" for c in CALLS)


def test_back_to_single_mode_restores_the_response_panel(pg):
    page = pg.page
    open_compare(page)
    if not page.locator("[data-pg-action=compare]").is_visible():
        page.click("[data-menu-toggle=pg-menu]")
    page.click("[data-pg-action=compare]")  # the same item turns it off
    expect(page.locator("#pg-root")).to_have_attribute("data-mode", "single")
    expect(page.locator("#pg-out")).to_be_visible()


def test_status_chips_and_stop_all(pg):
    page = pg.page
    open_compare(page)
    page.click("[data-add-variant]")
    columns = page.locator("[data-variant]")
    expect(columns.nth(0).locator("[data-v-status]")).to_have_text("Ready")
    for i in range(3):
        columns.nth(i).locator("[data-v-model]").fill("slow")
    expect(page.locator("[data-stop-all]")).to_be_disabled()
    page.click("[data-run-all]")
    for i in range(3):
        expect(columns.nth(i).locator("[data-v-status]")).to_have_text("Waiting")
    for i in range(3):
        expect(columns.nth(i).locator("[data-v-status]")).to_have_text("Streaming")
    expect(page.locator("[data-stop-all]")).to_be_enabled()
    page.click("[data-stop-all]")
    for i in range(3):
        expect(columns.nth(i).locator("[data-v-status]")).to_have_text("Stopped")
    expect(page.locator("[data-stop-all]")).to_be_disabled()
    deadline = time.time() + 5
    while len(CLOSED) < 3 and time.time() < deadline:
        time.sleep(0.05)
    assert len(CLOSED) == 3  # the provider saw every connection drop


def test_status_chips_report_done_and_error(pg):
    page = pg.page
    open_compare(page)
    columns = page.locator("[data-variant]")
    columns.nth(0).locator("[data-v-model]").fill(MODELS[0])
    columns.nth(1).locator("[data-v-model]").fill("boom")
    page.click("[data-run-all]")
    expect(columns.nth(0).locator("[data-v-status]")).to_have_text("Done")
    expect(columns.nth(1).locator("[data-v-status]")).to_have_text("Error")
    expect(columns.nth(1).locator("[data-v-status]")).to_have_attribute("data-state", "error")


def test_the_compare_menu_item_reads_exit_compare_while_compare_is_on(pg):
    page = pg.page
    page.click("[data-menu-toggle=pg-menu]")
    expect(page.locator("[data-pg-action=compare]")).to_have_text("Compare variants")
    page.click("[data-pg-action=compare]")
    expect(page.locator("#pg-root")).to_have_attribute("data-mode", "compare")
    page.click("[data-menu-toggle=pg-menu]")
    item = page.get_by_role("menuitem", name="Exit compare")
    expect(item).to_be_visible()
    item.click()
    expect(page.locator("#pg-root")).to_have_attribute("data-mode", "single")
    page.click("[data-menu-toggle=pg-menu]")
    expect(page.get_by_role("menuitem", name="Compare variants")).to_be_visible()


def cut_before_done(page) -> None:
    """Let the real run (real server, fake provider) through, but drop its final `done` event, as
    a proxy or a dropped connection would."""

    def handle(route):
        response = route.fetch()
        blocks = [b for b in response.text().split("\n\n") if b.strip()]
        assert blocks[-1].startswith("event: done"), blocks[-1]
        route.fulfill(response=response, body="\n\n".join(blocks[:-1]) + "\n\n")

    page.route("**/playground/run", handle)


def test_a_stream_without_done_says_it_ended_early(pg):
    page = pg.page
    cut_before_done(page)
    page.click("#pg-run")
    expect(page.locator("#pg-out")).to_contain_text(f"{MODELS[0]} says hello")
    expect(page.locator("#pg-out")).to_contain_text("The stream ended early")
    expect(page.locator("#pg-status")).to_have_text("Ended early")
    expect(page.locator("#pg-status")).not_to_have_text("Done")


def test_a_compare_column_without_done_says_it_ended_early(pg):
    page = pg.page
    cut_before_done(page)
    open_compare(page)
    columns = page.locator("[data-variant]")
    columns.nth(0).locator("[data-v-model]").fill(MODELS[0])
    columns.nth(1).locator("[data-v-model]").fill(MODELS[1])
    page.click("[data-run-all]")
    for i in range(2):
        col = columns.nth(i)
        expect(col.locator("[data-v-text]")).to_contain_text(f"{MODELS[i]} says hello")
        expect(col.locator("[data-v-status]")).to_have_text("Ended early")
        expect(col.locator("[data-v-status]")).to_have_attribute("data-state", "early")
        expect(col.locator("[data-v-error]")).to_contain_text("The stream ended early")
