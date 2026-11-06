"""Journey (AC-10): three variants with different models stream side by side, each with its own
usage and cost; a fifth cannot be added; the columns stack under 1100px.

The compare panel opens on the playground page's `pg:action` event and reads `window.Playground`.
Here a minimal host provides those hooks (`#pg-compare-mount`, `getState`, `setMode`), so the
script is exercised in a real browser on its own.
"""

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
        STARTS.append(time.monotonic())
        for word in (model, "says", "hello"):
            yield TextDelta(word + " ")
            time.sleep(0.25)
        size = 100 * (MODELS.index(model) + 1)
        yield UsageDelta(size, size * 2)
        yield DoneDelta("stop")


HOST = """
document.body.innerHTML = '<main><div id="pg-grid"></div>'
  + '<div id="pg-compare-mount" hidden></div></main>';
window.Playground = {
  config: { projectId: '%(pid)s' },
  getState: function () { return {
    connectionId: '%(conn)s', model: '%(model)s',
    messages: [{ role: 'user', content: 'Say hello' }], variables: {}, params: {}, tools: [],
    responseSchema: null }; },
  setMode: function (mode) {
    document.getElementById('pg-grid').hidden = mode === 'compare';
    document.getElementById('pg-compare-mount').hidden = mode !== 'compare'; }
};
"""


def open_compare(page):
    page.evaluate(
        "document.dispatchEvent(new CustomEvent('pg:action', { detail: { action: 'compare' } }))"
    )


def load(page, src):
    page.evaluate(
        """src => new Promise((ok, fail) => { const s = document.createElement('script');
        s.src = src; s.onload = ok; s.onerror = fail; document.head.appendChild(s); })""",
        src,
    )


@pytest.fixture
def compare_page(session, live):
    providers.register("anthropic", PerModel)
    STARTS.clear()
    with live.container.session_factory() as db:
        owner = db.scalar(select(User).where(User.email == live.account.email))
        project = db.get(Project, live.account.project_id)
        conn = connections.create(
            db, project, Actor.for_user(owner, Role.OWNER),
            ConnectionPolicy.from_settings(live.container.settings),
            provider="anthropic", name="Claude fake", secrets={"api_key": "sk-ant-fake-123456"},
        )  # fmt: skip
        db.commit()
        conn_id = conn.id
    page = session.page
    pid = live.account.project_id
    page.goto(f"{live.url}/project/{pid}/traces")
    page.add_style_tag(url="/static/playground_compare.css")
    page.evaluate("() => { " + HOST % {"pid": pid, "conn": conn_id, "model": MODELS[0]} + " }")
    load(page, "/static/playground_compare.js")
    yield session
    providers.unregister("anthropic")


def test_three_variants_stream_independently(compare_page, live):
    page = compare_page.page
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
    assert compare_page.problems == []


def test_a_fifth_variant_cannot_be_added(compare_page):
    page = compare_page.page
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


def test_columns_sit_side_by_side_wide_and_stack_under_1100px(compare_page):
    page = compare_page.page
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


def test_errors_and_stop_are_per_column(compare_page):
    page = compare_page.page
    open_compare(page)
    columns = page.locator("[data-variant]")
    columns.nth(0).locator("[data-v-model]").fill(MODELS[0])
    columns.nth(1).locator("[data-v-model]").fill("")  # falls back to the shared model
    columns.nth(1).locator("[data-v-follow]").uncheck()
    for row in columns.nth(1).locator(".v-msg [data-m-remove]").all():
        row.click()  # no messages of its own: this column cannot run
    page.click("[data-run-all]")
    expect(columns.nth(1).locator("[data-v-error]")).to_be_visible()
    expect(columns.nth(0).locator("[data-v-text]")).to_contain_text(f"{MODELS[0]} says hello")
