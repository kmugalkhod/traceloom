"""AC-10 and the playground run API: SSE events, usage, cost, rate limit, scrubbing, save."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from sqlalchemy import select

from tests.unit.providers.conftest import _script
from tests.unit.providers.test_anthropic import events as anthropic_events
from traceloom.auth.permissions import Role
from traceloom.db.models import Prompt, PromptVersion
from traceloom.llm import connections, providers
from traceloom.llm.connections import ConnectionPolicy
from traceloom.llm.crypto import generate_key
from traceloom.llm.providers import (
    BaseProvider,
    DoneDelta,
    ErrorDelta,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
)
from traceloom.llm.providers.anthropic import AnthropicProvider
from traceloom.llm.run import Run, RunSpec
from traceloom.prompts.service import Actor

KEY = "sk-ant-api03-playgroundsecret0001"
MESSAGES = [{"role": "user", "content": "Say hi to {{who}}"}]


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        name, _, data = block.partition("\n")
        out.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


@pytest.fixture(autouse=True)
def restore_registry():
    yield
    for name in providers.PROVIDERS:
        providers.unregister(name)


@pytest.fixture
def conn_id(db, world, container):
    admin = Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)
    policy = ConnectionPolicy.from_settings(container.settings)
    row = connections.create(
        db, world.project, admin, policy, provider="anthropic", name="Claude",
        secrets={"api_key": KEY},
    )  # fmt: skip
    db.commit()
    return row.id


@pytest.fixture
def member_client(client, login, world):
    member = login(client, world.users[Role.MEMBER])  # with the CSRF header like app.js
    member.headers["Accept"] = "application/json"  # the page's fetch asks for JSON errors
    return member


def url(world, tail):
    return f"/project/{world.project.id}/playground/{tail}"


def body(conn_id, **extra):
    return {
        "connection_id": conn_id,
        "model": "claude-haiku-4-5-20251001",
        "messages": MESSAGES,
        "variables": {"who": "Ada"},
        **extra,
    }


def test_deltas_tool_calls_usage_and_done_stream(member_client, world, conn_id, fake_provider):
    providers.register("anthropic", fake_provider)
    fake_provider.script(
        TextDelta("Hel"), TextDelta("lo"), ToolCallDelta("t1", "weather", {"city": "Paris"}),
        UsageDelta(1000, 200, {"cache_read": 10}), DoneDelta("tool_use"),
    )  # fmt: skip
    r = member_client.post(url(world, "run"), json=body(conn_id))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    got = parse_sse(r.text)
    assert [n for n, _ in got] == ["delta", "delta", "tool_call", "usage", "done"]
    assert got[2][1] == {"id": "t1", "name": "weather", "arguments": {"city": "Paris"}}
    usage = got[3][1]
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (
        1000,
        200,
        1200,
    )
    assert usage["cost"] > 0 and usage["cost_source"] == "calculated"
    assert usage["ttft_ms"] is not None and usage["latency_ms"] >= usage["ttft_ms"]
    assert got[4][1]["finish_reason"] == "tool_use"
    sent = fake_provider.calls[0]["messages"]
    assert sent == [{"role": "user", "content": "Say hi to Ada"}]  # variables rendered


def test_anthropic_request_matches_and_streams(member_client, world, conn_id):
    script = _script("anthropic")
    script.reply(**anthropic_events(("text", ["Hi ", "Ada"])))
    providers.register("anthropic", lambda cfg: AnthropicProvider(cfg, http_client=script.http()))
    tools = [{"name": "weather", "description": "Get weather", "parameters": {"type": "object"}}]
    r = member_client.post(
        url(world, "run"),
        json=body(conn_id, params={"temperature": 0.2, "max_tokens": 300}, tools=tools),
    )
    got = parse_sse(r.text)
    assert [n for n, _ in got] == ["delta", "delta", "usage", "done"]
    sent = script.body
    assert sent["model"] == "claude-haiku-4-5-20251001"
    assert sent["messages"] == [{"role": "user", "content": "Say hi to Ada"}]
    assert (sent["temperature"], sent["max_tokens"]) == (0.2, 300)
    assert sent["tools"] == [{"name": "weather", "description": "Get weather",
                              "input_schema": {"type": "object"}}]  # fmt: skip
    usage = got[2][1]
    assert usage["input_tokens"] == 12 and usage["output_tokens"] == 6 and usage["cost"] > 0


def test_provider_errors_are_inline_and_scrubbed(member_client, world, conn_id):
    class Leaky(BaseProvider):
        def _stream(self, *args):
            yield TextDelta("partial")
            yield ErrorDelta(f"upstream said: bad key {self.cfg.api_key}")

    providers.register("anthropic", Leaky)
    got = parse_sse(member_client.post(url(world, "run"), json=body(conn_id)).text)
    names = [n for n, _ in got]
    assert names == ["delta", "error", "usage", "done"]
    assert "bad key" in got[1][1]["message"] and KEY not in json.dumps(got)
    assert got[-1][1]["finish_reason"] == "error"


def test_exception_in_provider_is_inline_and_scrubbed(member_client, world, conn_id):
    class Crashes(BaseProvider):
        def _stream(self, *args):
            raise RuntimeError(f"connect failed with {self.cfg.api_key}")
            yield

    providers.register("anthropic", Crashes)
    text = member_client.post(url(world, "run"), json=body(conn_id)).text
    assert "RuntimeError" in text and KEY not in text


def test_over_the_rate_limit_returns_429(
    member_client, world, conn_id, fake_provider, container, monkeypatch
):
    providers.register("anthropic", fake_provider)
    monkeypatch.setattr(container.settings, "playground_rate", "2/minute")
    codes = [
        member_client.post(url(world, "run"), json=body(conn_id)).status_code for _ in range(3)
    ]
    assert codes == [200, 200, 429]
    r = member_client.post(url(world, "run"), json=body(conn_id))
    assert (
        "limit" in r.json()["error"]["message"].lower()
        and "2/minute" in r.json()["error"]["message"]
    )
    assert int(r.headers["retry-after"]) >= 1


def test_csrf_and_roles(client, login, world, conn_id, fake_provider):
    providers.register("anthropic", fake_provider)
    no_csrf = login(client, world.users[Role.MEMBER], csrf=False)
    assert no_csrf.post(url(world, "run"), json=body(conn_id)).status_code == 403
    viewer = login(client, world.users[Role.VIEWER], csrf=True)
    assert viewer.post(url(world, "run"), json=body(conn_id)).status_code == 403
    assert viewer.post(url(world, "save"), json={}).status_code in (403, 422)


def test_unknown_or_foreign_connection(member_client, world, conn_id, make_project):
    assert member_client.post(url(world, "run"), json=body("nope")).status_code == 404
    other = make_project(world.org)
    r = member_client.post(f"/project/{other.id}/playground/run", json=body(conn_id))
    assert r.status_code == 404  # the connection belongs to another project


def test_unreadable_key_asks_for_reentry(member_client, world, conn_id, container, monkeypatch):
    monkeypatch.setattr(
        container.settings,
        "encryption_key",
        generate_key(),
    )
    r = member_client.post(url(world, "run"), json=body(conn_id))
    assert r.status_code == 400 and "re-entry" in r.json()["error"]["message"]


def test_trace_this_run_ingests_in_the_playground_environment(
    member_client, world, conn_id, fake_provider, container
):
    providers.register("anthropic", fake_provider)
    fake_provider.script(TextDelta("done"), UsageDelta(100, 50), DoneDelta("end_turn"))
    r = member_client.post(
        url(world, "run"), json=body(conn_id, trace=True, prompt_name="support", prompt_version=2)
    )
    trace_id = parse_sse(r.text)[-1][1]["trace_id"]
    container.queue.drain()
    detail = container.trace_store.get_trace(world.project.id, trace_id)
    gen = next(o for o in detail.observations if o.type == "generation")
    assert detail.trace.environment == "playground" and gen.environment == "playground"
    assert (gen.prompt_name, gen.prompt_version) == ("support", 2)
    assert gen.usage_details["input"] == 100 and gen.cost_source == "calculated"
    assert gen.output == "done"
    # without the toggle nothing is stored
    r = member_client.post(url(world, "run"), json=body(conn_id))
    assert "trace_id" not in parse_sse(r.text)[-1][1]


def test_output_is_json_in_a_non_html_stream(member_client, world, conn_id, fake_provider):
    providers.register("anthropic", fake_provider)
    fake_provider.script(TextDelta("<img src=x onerror=alert(1)>"), DoneDelta("stop"))
    r = member_client.post(url(world, "run"), json=body(conn_id))
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert parse_sse(r.text)[0][1]["text"] == "<img src=x onerror=alert(1)>"


def test_concurrent_variants_independent_usage(container, world):
    class Scripted(BaseProvider):
        def __init__(self, tokens):
            super().__init__(providers.ProviderConfig())
            self.tokens = tokens

        def _stream(self, *args):
            for i in range(20):
                yield TextDelta(f"{self.tokens}-{i} ")
                time.sleep(0.001)
            yield UsageDelta(self.tokens, self.tokens * 2)
            yield DoneDelta("stop")

    results: dict[int, list] = {}

    def worker(tokens):
        spec = RunSpec(
            messages=[{"role": "user", "content": "x"}], model="claude-haiku-4-5-20251001"
        )
        results[tokens] = list(Run(container, world.project.id, Scripted(tokens), spec).events())

    threads = [threading.Thread(target=worker, args=(n,)) for n in (100, 200, 300, 400)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    for tokens, evs in results.items():
        usage = next(e.data for e in evs if e.name == "usage")
        assert (usage["input_tokens"], usage["output_tokens"]) == (tokens, tokens * 2)
        assert all(e.data["text"].startswith(f"{tokens}-") for e in evs if e.name == "delta")


class Tracked(BaseProvider):
    """Yields slowly and records whether its stream was closed."""

    closed = threading.Event()
    first = threading.Event()
    release = threading.Event()
    ran_on = threading.Event()

    def _stream(self, *args):
        try:
            yield TextDelta("one")
            Tracked.first.set()
            Tracked.release.wait(5)
            yield TextDelta("two")
            Tracked.ran_on.set()  # only reached if the consumer kept pulling
            yield DoneDelta("stop")
        finally:
            Tracked.closed.set()


@pytest.fixture
def tracked():
    for event in (Tracked.closed, Tracked.first, Tracked.release, Tracked.ran_on):
        event.clear()
    return Tracked


def test_cancelling_a_run_closes_the_provider_stream(container, world, tracked):
    spec = RunSpec(messages=[{"role": "user", "content": "x"}], model="m")
    run = Run(container, world.project.id, Tracked(providers.ProviderConfig()), spec)
    events = run.events()
    assert next(events).name == "delta"
    run.cancel()  # the client went away between two events
    assert tracked.closed.is_set()
    assert list(events) == []


def test_client_disconnect_stops_the_run(app, client, login, world, conn_id, tracked):
    providers.register("anthropic", Tracked)
    member = login(client, world.users[Role.MEMBER])
    cookie = "; ".join(f"{k}={v}" for k, v in member.cookies.items())
    payload = json.dumps(body(conn_id)).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": url(world, "run"), "raw_path": url(world, "run").encode(),
        "query_string": b"", "root_path": "", "server": ("testserver", 80),
        "client": ("1.2.3.4", 1),
        "headers": [
            (b"host", b"testserver"), (b"cookie", cookie.encode()),
            (b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()),
            (b"x-csrf-token", member.headers["X-CSRF-Token"].encode()),
        ],
        "state": {},
    }  # fmt: skip

    async def drive():
        sent_body = False
        got_chunk = asyncio.Event()

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await got_chunk.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                got_chunk.set()

        await asyncio.wait_for(app(scope, receive, send), 10)

    asyncio.run(drive())
    tracked.release.set()  # the provider would have kept going
    assert tracked.closed.wait(5)
    assert not tracked.ran_on.is_set()  # the run stopped pulling once the client was gone


def test_save_creates_a_new_prompt_version(member_client, world, db):
    msgs = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "{{q}}"}]
    r = member_client.post(
        url(world, "save"),
        json={"name": "pg/support", "messages": msgs, "commit_message": "from playground",
              "model": "gpt-4o", "params": {"temperature": 0.1}},
    )  # fmt: skip
    assert r.status_code == 201 and r.json() == {"name": "pg/support", "version": 1}
    r = member_client.post(
        url(world, "save"),
        json={"name": "pg/support", "messages": msgs[:1], "commit_message": "v2"},
    )
    assert r.json()["version"] == 2
    prompt = db.scalars(select(Prompt).where(Prompt.name == "pg/support")).one()
    rows = db.scalars(select(PromptVersion).where(PromptVersion.prompt_id == prompt.id)).all()
    assert sorted(v.version for v in rows) == [1, 2]
    v1 = next(v for v in rows if v.version == 1)
    assert v1.content == msgs and v1.config["model"] == "gpt-4o"
    bad = member_client.post(
        url(world, "save"), json={"name": "x", "messages": [], "commit_message": " "}
    )
    assert bad.status_code == 400
    prod = member_client.post(
        url(world, "save"),
        json={
            "name": "pg/support",
            "messages": msgs,
            "commit_message": "go",
            "labels": ["production"],
        },
    )
    assert prod.status_code == 403  # labels follow the protected-label rules
