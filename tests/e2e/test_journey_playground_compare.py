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


class PerModel(BaseProvider):
    """Streams the model name, slowly, with usage that depends on the model."""

    def _stream(self, messages, model, params, tools, schema):
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
    expect(page.locator("#pg-grid")).to_be_hidden()


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
