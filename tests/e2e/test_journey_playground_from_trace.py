"""Journey: open the playground from a traced generation, run it on a fake provider, watch
the stream and its stats, stop a run with Esc, and check hostile text only ever shows as text."""

from __future__ import annotations

import threading
import time

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import Project, User
from traceloom.llm import connections, providers
from traceloom.llm.connections import ConnectionPolicy
from traceloom.llm.providers import DoneDelta, ErrorDelta, TextDelta, UsageDelta
from traceloom.prompts.service import Actor

pytestmark = pytest.mark.e2e

KEY = "sk-ant-e2e-SECRET-0001"
SCRIPT = "<script>alert(1)</script>"
IMG = "<img src=x onerror=alert(1)>"


class Scripted:
    """A provider that records its calls and plays one of a few behaviours."""

    name = "fake"

    def __init__(self) -> None:
        self.mode = "echo"
        self.calls: list[dict] = []
        self.chunks_sent = 0
        self.closed = threading.Event()
        self.output = ""

    def __call__(self, config):
        return self

    def stream(self, messages, model, params=None, tools=None, response_schema=None):
        self.calls.append(
            {"messages": list(messages), "model": model, "params": dict(params or {})}
        )
        self.closed.clear()
        try:
            if self.mode == "slow":
                for i in range(400):
                    time.sleep(0.05)
                    self.chunks_sent += 1
                    yield TextDelta(f"word{i} ")
            elif self.mode == "error":
                yield ErrorDelta(f"401 invalid key {KEY}")
                yield DoneDelta("error")
            elif self.mode == "refusal":
                yield ErrorDelta("The model declined to answer this request.")
                yield DoneDelta("refusal")
            else:
                text = self.output or "Echo: " + messages[-1]["content"]
                for part in (text[: len(text) // 2], text[len(text) // 2 :]):
                    time.sleep(0.05)
                    yield TextDelta(part)
                yield UsageDelta(input_tokens=120, output_tokens=30)
                yield DoneDelta("stop")
        finally:
            self.closed.set()


@pytest.fixture
def fake(live):
    provider = Scripted()
    providers.register("anthropic", provider)
    with live.container.session_factory() as db:
        owner = db.scalar(select(User).where(User.email == live.account.email))
        project = db.get(Project, live.account.project_id)
        connections.create(
            db,
            project,
            Actor.for_user(owner, Role.OWNER),
            ConnectionPolicy.from_settings(live.container.settings),
            provider="anthropic",
            name="Fake Claude",
            secrets={"api_key": KEY},
        )
        db.commit()
    yield provider
    providers.unregister("anthropic")


def playground(live, query=""):
    return f"{live.url}/project/{live.account.project_id}/playground{query}"


def run_with_keyboard(page):
    page.locator("[data-msg-text]").first.focus()
    page.keyboard.press("Control+Enter")


def test_from_trace_run_streams_text_with_usage(live, session, fake, events):
    live.ingest(events(1, input_="question 1"))
    page = session.page
    page.goto(playground(live, "?trace=trace-001&obs=trace-001-gen"))

    expect(page.locator("#pg-note")).to_contain_text("Opened from trace trace-001")
    expect(page.locator("#pg-model")).to_have_value("claude-sonnet-4-5")
    expect(page.locator("#pg-connection")).to_have_value(  # the matching connection was chosen
        page.locator("#pg-connection option", has_text="Fake Claude").get_attribute("value")
    )
    area = page.locator("[data-msg-text]").first
    expect(area).to_have_value("question 1")

    area.fill("Say hi to {{who}}")
    expect(page.locator("#pg-vars label")).to_have_text(["who"])
    page.locator("#pg-vars input").fill("Ada")
    run_with_keyboard(page)

    out = page.locator("#pg-out")
    expect(out).to_contain_text("Echo: Say hi to Ada")
    expect(page.locator("#pg-status")).to_contain_text("Done")
    usage = page.locator("#pg-usage")
    expect(usage.locator("[data-u=input_tokens]")).to_have_text("120")
    expect(usage.locator("[data-u=output_tokens]")).to_have_text("30")
    expect(usage.locator("[data-u=total_tokens]")).to_have_text("150")
    expect(usage.locator("[data-u=cost]")).to_contain_text("$")
    expect(usage.locator("[data-u=latency_ms]")).not_to_have_text("-")
    expect(usage.locator("[data-u=ttft_ms]")).not_to_have_text("-")
    assert fake.calls[-1]["messages"][-1]["content"] == "Say hi to Ada"  # variables compiled
    assert fake.calls[-1]["model"] == "claude-sonnet-4-5"
    expect(page.locator("#pg-run")).to_be_visible()
    assert session.problems == []


def test_esc_stops_the_stream_and_the_provider_is_released(live, session, fake):
    fake.mode = "slow"
    page = session.page
    page.goto(playground(live))
    page.locator("[data-msg-text]").nth(1).fill("go")
    page.click("#pg-run")
    expect(page.locator("#pg-stop")).to_be_visible()
    expect(page.locator("#pg-out")).to_contain_text("word1")
    expect(page.locator("#pg-status")).to_contain_text("Streaming")

    page.keyboard.press("Escape")
    expect(page.locator("#pg-status")).to_contain_text("Stopped")
    expect(page.locator("#pg-run")).to_be_visible()
    assert fake.closed.wait(5), "the provider stream was not closed after Stop"
    sent = fake.chunks_sent
    page.wait_for_timeout(400)
    assert fake.chunks_sent <= sent + 1  # it stopped producing
    shown = page.locator("#pg-out").inner_text()
    page.wait_for_timeout(300)
    assert page.locator("#pg-out").inner_text() == shown  # and the page stopped growing


def test_stop_button_stops_too(live, session, fake):
    fake.mode = "slow"
    page = session.page
    page.goto(playground(live))
    page.locator("[data-msg-text]").nth(1).fill("go")
    page.click("#pg-run")
    expect(page.locator("#pg-out")).to_contain_text("word0")
    page.click("#pg-stop")
    expect(page.locator("#pg-status")).to_contain_text("Stopped")


def test_hostile_text_is_only_ever_text(live, session, fake):
    fake.output = f"{SCRIPT}\n{IMG}"
    page = session.page
    page.goto(playground(live))
    area = page.locator("[data-msg-text]").nth(1)
    area.fill(f"{SCRIPT} {IMG} {{{{x}}}}")
    page.locator("#pg-vars input").fill(SCRIPT)
    run_with_keyboard(page)
    expect(page.locator("#pg-out")).to_contain_text("alert(1)")
    expect(page.locator("#pg-status")).to_contain_text("Done")
    for where in ("#pg-out", "#pg-msgs", "#pg-vars"):
        assert page.locator(f"{where} script, {where} img").count() == 0, where
    assert "<script>" in page.locator("#pg-out").inner_text()  # shown literally
    # A hostile tool definition and schema are only parsed as JSON, never rendered.
    page.click("#pg-settings-btn")
    page.click("[data-tab=tools]")
    page.fill("#pg-tools", f'[{{"name": "{SCRIPT}"}}]')
    assert page.locator("#pg-drawer script, #pg-drawer img").count() == 0
    page.wait_for_timeout(300)
    assert [p for p in session.problems if p.startswith(("dialog", "pageerror"))] == []


def test_provider_error_never_shows_the_key(live, session, fake):
    fake.mode = "error"
    page = session.page
    page.goto(playground(live))
    page.locator("[data-msg-text]").nth(1).fill("hi")
    page.click("#pg-run")
    out = page.locator("#pg-out")
    expect(out).to_contain_text("The provider returned an error")
    expect(page.locator("#pg-status")).to_contain_text("Failed")
    assert KEY not in page.content() and "SECRET" not in out.inner_text()


def test_refusal_is_shown_as_a_declined_answer(live, session, fake):
    fake.mode = "refusal"
    page = session.page
    page.goto(playground(live))
    page.locator("[data-msg-text]").nth(1).fill("hi")
    page.click("#pg-run")
    expect(page.locator("#pg-out")).to_contain_text("The model declined to answer")
    expect(page.locator("#pg-status")).to_contain_text("Declined")


def test_run_without_a_connection_says_so_and_admins_get_a_link(live, session):
    page = session.page
    page.goto(playground(live))
    expect(page.get_by_role("link", name="Add an LLM connection")).to_have_attribute(
        "href", f"/project/{live.account.project_id}/settings/connections"
    )


def test_params_follow_the_model(live, session, fake):
    page = session.page
    page.goto(playground(live))
    page.fill("#pg-model", "claude-sonnet-5-5")
    page.click("#pg-settings-btn")
    expect(page.locator("#pg-params input")).to_have_count(1)  # only max tokens
    expect(page.locator("#pg-params-hint")).to_contain_text("hidden because claude-sonnet-5-5")
    page.fill("#pg-model", "claude-haiku-4-5-20251001")
    page.locator("#pg-model").dispatch_event("change")
    expect(page.locator("#pg-params input")).to_have_count(4)
    page.fill("#pg-param-temperature", "0.2")
    page.locator("[data-msg-text]").nth(1).fill("hi")
    page.click("#pg-run")
    expect(page.locator("#pg-status")).to_contain_text("Done")
    assert fake.calls[-1]["params"] == {"temperature": 0.2}


def test_tool_json_errors_are_inline_and_not_applied(live, session, fake):
    page = session.page
    page.goto(playground(live))
    page.click("#pg-settings-btn")
    page.click("[data-tab=tools]")
    page.fill("#pg-tools", '[{"name": "lookup", "parameters": {')
    expect(page.locator("#pg-tools-err")).to_be_visible()
    expect(page.locator("#pg-tools-err")).to_contain_text("not applied")
    page.fill("#pg-tools", '[{"name": "lookup", "parameters": {"type": "object"}}]')
    expect(page.locator("#pg-tools-err")).to_be_hidden()
    page.locator("[data-msg-text]").nth(1).fill("hi")
    page.click("#pg-run")
    expect(page.locator("#pg-status")).to_contain_text("Done")


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(("width", "height"), [(390, 800), (1280, 900)])
def test_layout_has_no_horizontal_scroll(live, session, fake, theme, width, height):
    page = session.page
    page.set_viewport_size({"width": width, "height": height})
    page.goto(playground(live))
    page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
    page.click("#pg-settings-btn")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    for button in ("#pg-run", "#pg-settings-btn"):
        box = page.locator(button).bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= width + 1


def demo_answer(live):
    """Run the demo agent against the live server; return (trace_id, observation_id, messages)."""
    import traceloom_sdk
    from traceloom_sdk import Traceloom, set_default_client

    from examples import agent

    client = Traceloom(
        host=live.url,
        public_key=live.account.public_key,
        secret_key=live.account.secret_key,
        flush_interval=0.1,
    )
    set_default_client(client)
    try:
        result = agent.run()
        assert traceloom_sdk.flush() is True
    finally:
        set_default_client(None)
        client.shutdown(2)
    trace_id = result["trace_id"]
    deadline = time.time() + 10
    while time.time() < deadline:
        detail = live.container.trace_store.get_trace(live.account.project_id, trace_id)
        answers = [o for o in (detail.observations if detail else []) if o.name == "answer"]
        if answers:
            return trace_id, answers[0].id, answers[0].input
        time.sleep(0.2)
    raise AssertionError("the demo agent's answer generation never arrived")


def test_replay_carries_every_message_including_tool_results(live, session, fake):
    trace_id, obs_id, original = demo_answer(live)
    assert len(original) == 7 and sum(m["role"] == "tool" for m in original) == 3
    page = session.page
    page.goto(playground(live, f"?trace={trace_id}&obs={obs_id}"))

    cards = page.locator("#pg-msgs [data-msg]")
    expect(cards).to_have_count(7)
    roles = [page.locator("#pg-msgs [data-msg-role]").nth(i).input_value() for i in range(7)]
    assert roles == [m["role"] for m in original]
    areas = page.locator("#pg-msgs [data-msg-text]")
    for i, m in enumerate(original):
        expect(areas.nth(i)).to_have_value(m["content"])
    expect(page.locator("#pg-note")).to_contain_text("7 messages")
    expect(page.locator("#pg-note")).to_contain_text("3 tool results")

    page.click("#pg-run")
    expect(page.locator("#pg-status")).to_contain_text("Done")
    sent = fake.calls[-1]["messages"]
    assert [(m["role"], m["content"]) for m in sent] == [
        (m["role"], m["content"]) for m in original
    ]


def test_tool_calls_and_ids_survive_editing_and_run(live, session, fake):
    page = session.page
    page.goto(playground(live))
    page.evaluate(
        """() => Playground.setState({messages: [
            {role: 'user', content: 'weather?'},
            {role: 'assistant', content: '', tool_calls: [
                {id: 'c1', name: 'get_weather', arguments: {city: 'Oslo'}}]},
            {role: 'tool', content: '4 degrees', tool_call_id: 'c1'}]})"""
    )
    expect(page.locator("#pg-msgs [data-msg]")).to_have_count(3)
    calls = page.locator("#pg-msgs [data-msg-calls]").nth(1)  # the assistant card
    expect(calls).to_have_value(__import__("re").compile("get_weather"))
    expect(page.locator("#pg-msgs [data-msg-callid]").nth(2)).to_have_value("c1")
    calls.fill('[{"id": "c1", "name": "get_weather", "arguments": {"city": "Bergen"}}]')
    page.click("#pg-run")
    expect(page.locator("#pg-status")).to_contain_text("Done")
    sent = fake.calls[-1]["messages"]
    assert sent[1]["tool_calls"] == [
        {"id": "c1", "name": "get_weather", "arguments": {"city": "Bergen"}}
    ]
    assert sent[2]["tool_call_id"] == "c1" and sent[2]["role"] == "tool"
    calls.fill("[{broken")
    expect(page.locator("#pg-msgs [data-msg-callserr]").nth(1)).to_be_visible()


def test_a_message_that_cannot_be_shown_is_reported_not_dropped(live, session, fake):
    trace_id = "trace-odd"
    odd = [
        {"role": "user", "content": "hello"},
        {"role": "function", "content": "legacy function result"},
    ]
    live.ingest(
        [
            {
                "id": "odd-1",
                "type": "generation-create",
                "timestamp": "2026-11-06T10:00:00.000Z",
                "body": {
                    "id": "odd-gen",
                    "traceId": trace_id,
                    "name": "odd",
                    "model": "claude-sonnet-5-5",
                    "startTime": "2026-11-06T10:00:00.000Z",
                    "input": odd,
                    "output": "x",
                },
            }
        ]
    )
    page = session.page
    page.goto(playground(live, f"?trace={trace_id}&obs=odd-gen"))
    note = page.locator("#pg-note")
    expect(note).to_contain_text("could not be shown")
    expect(note).to_contain_text("function")
    expect(note).to_contain_text("legacy function result")  # the text is preserved in the note


@pytest.mark.parametrize(
    ("model", "inputs"),
    [
        ("claude-sonnet-5-5", 1),
        ("claude-sonnet-5", 1),
        ("claude-opus-5", 1),
        ("claude-fable-5", 1),
        ("claude-fable-5-1", 1),
        ("claude-sonnet-5-5-20260301", 1),
        ("claude-haiku-4-5-20251001", 4),
        ("claude-opus-4-8", 4),
    ],
)
def test_parameters_offered_follow_the_server_rules(live, session, fake, model, inputs):
    page = session.page
    page.goto(playground(live))
    page.click("#pg-settings-btn")
    page.fill("#pg-model", model)
    page.locator("#pg-model").dispatch_event("change")
    expect(page.locator("#pg-params input")).to_have_count(inputs)
    if inputs == 1:
        expect(page.locator("#pg-params-hint")).to_contain_text("temperature")


def test_with_several_connections_the_user_picks_and_it_is_remembered(live, session, fake):
    with live.container.session_factory() as db:
        owner = db.scalar(select(User).where(User.email == live.account.email))
        project = db.get(Project, live.account.project_id)
        connections.create(
            db, project, Actor.for_user(owner, Role.OWNER),
            ConnectionPolicy.from_settings(live.container.settings),
            provider="anthropic", name="Second connection", secrets={"api_key": "sk-second-0001"},
        )  # fmt: skip
        db.commit()
    page = session.page
    page.goto(playground(live))
    expect(page.locator("#pg-connection")).to_have_value("")  # no silent default
    page.locator("[data-msg-text]").nth(1).fill("hi")
    page.keyboard.press("Control+Enter")  # the button is marked disabled; the shortcut explains why
    expect(page.locator("#pg-out")).to_contain_text("Pick a connection first")
    assert fake.calls == []  # nothing was sent anywhere

    page.select_option("#pg-connection", label="Second connection")
    page.wait_for_timeout(300)  # the choice is saved in the background
    page.goto(playground(live))
    expect(page.locator("#pg-connection option:checked")).to_have_text("Second connection")
