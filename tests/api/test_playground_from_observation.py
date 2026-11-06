"""FR-L5: "open in playground" pre-fill equals the traced generation."""

from __future__ import annotations

import pytest

from tests.helpers_web import T0
from tests.unit.providers.conftest import _script
from tests.unit.providers.test_anthropic import events as anthropic_events
from traceloom.auth.permissions import Role
from traceloom.llm import connections, providers
from traceloom.llm.connections import ConnectionPolicy
from traceloom.llm.providers.anthropic import AnthropicProvider
from traceloom.ports import ObservationRow, TraceRow
from traceloom.prompts.service import Actor

INPUT = [
    {"role": "user", "content": "What is the weather in Paris?"},
    {"role": "assistant", "content": [
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "tu1", "name": "weather", "input": {"city": "Paris"}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu1", "content": "sunny"}]},
]  # fmt: skip
PARAMS = {
    "temperature": 0.2, "max_tokens": 300, "system": "Be brief.",
    "tools": [{"name": "weather", "description": "Get weather",
               "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}],
}  # fmt: skip


@pytest.fixture(autouse=True)
def restore_registry():
    yield
    for name in providers.PROVIDERS:
        providers.unregister(name)


@pytest.fixture
def generation(container, world):
    trace = TraceRow(id="t1", timestamp=T0, name="t1", event_ts=T0)
    obs = ObservationRow(
        id="g1", trace_id="t1", start_time=T0, type="generation", model="claude-haiku-4-5-20251001",
        input=INPUT, model_parameters=PARAMS, prompt_name="support", prompt_version=3, event_ts=T0,
    )  # fmt: skip
    container.trace_store.upsert(world.project.id, [trace], [obs], [])
    return obs


@pytest.fixture
def conn_id(db, world, container):
    admin = Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)
    row = connections.create(
        db, world.project, admin, ConnectionPolicy.from_settings(container.settings),
        provider="anthropic", name="Claude", secrets={"api_key": "sk-ant-key-000011112222"},
    )  # fmt: skip
    db.commit()
    return row.id


def fetch(client, world, obs="g1", trace="t1"):
    return client.get(
        f"/project/{world.project.id}/playground/from-observation/{obs}", params={"trace_id": trace}
    )


def test_prefill_matches_generation(login, client, world, generation, conn_id):
    member = login(client, world.users[Role.MEMBER])
    r = fetch(member, world)
    assert r.status_code == 200
    fill = r.json()
    assert fill["model"] == "claude-haiku-4-5-20251001" and fill["provider"] == "anthropic"
    assert fill["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "What is the weather in Paris?"},
        {"role": "assistant", "content": "Checking.",
         "tool_calls": [{"id": "tu1", "name": "weather", "arguments": {"city": "Paris"}}]},
        {"role": "tool", "tool_call_id": "tu1", "content": "sunny"},
    ]  # fmt: skip
    assert fill["params"] == {"temperature": 0.2, "max_tokens": 300}
    assert fill["tools"][0]["name"] == "weather"
    assert fill["tools"][0]["parameters"] == PARAMS["tools"][0]["input_schema"]
    assert (fill["prompt_name"], fill["prompt_version"]) == ("support", 3)
    assert fill["connection_id"] == conn_id
    assert [c["name"] for c in fill["connections"]] == ["Claude"]
    assert "sk-ant" not in r.text


def test_rerun_sends_the_source_request_to_the_provider(login, client, world, generation, conn_id):
    member = login(client, world.users[Role.MEMBER])
    fill = fetch(member, world).json()
    script = _script("anthropic")
    script.reply(**anthropic_events(("text", ["ok"])))
    providers.register("anthropic", lambda cfg: AnthropicProvider(cfg, http_client=script.http()))
    run = {k: fill[k] for k in ("model", "messages", "params", "tools", "connection_id")}
    r = member.post(f"/project/{world.project.id}/playground/run", json=run)
    assert r.status_code == 200
    sent = script.body
    assert sent["model"] == "claude-haiku-4-5-20251001" and sent["system"] == "Be brief."
    assert sent["temperature"] == 0.2 and sent["max_tokens"] == 300
    assert sent["tools"] == PARAMS["tools"]
    assert sent["messages"] == [
        {"role": "user", "content": "What is the weather in Paris?"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "tu1", "name": "weather", "input": {"city": "Paris"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "sunny"}]},
    ]  # fmt: skip


def test_openai_shaped_generation(container, login, client, world):
    trace = TraceRow(id="t2", timestamp=T0, name="t2", event_ts=T0)
    obs = ObservationRow(
        id="g2", trace_id="t2", start_time=T0, type="generation", model="gpt-4o", event_ts=T0,
        input=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
               {"role": "assistant", "content": None, "tool_calls": [
                   {"id": "c1", "type": "function",
                    "function": {"name": "f", "arguments": '{"a": 1}'}}]},
               {"role": "tool", "tool_call_id": "c1", "content": "r"}],
        model_parameters={"temperature": 0, "max_completion_tokens": 50, "stop": "END",
                          "tools": [{"type": "function", "function": {
                              "name": "f", "description": "d", "parameters": {"type": "object"}}}]},
    )  # fmt: skip
    container.trace_store.upsert(world.project.id, [trace], [obs], [])
    fill = fetch(login(client, world.users[Role.MEMBER]), world, "g2", "t2").json()
    assert fill["provider"] == "openai" and fill["connection_id"] is None
    assert fill["messages"][2] == {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "name": "f", "arguments": {"a": 1}}],
    }  # fmt: skip
    assert fill["params"] == {"temperature": 0, "max_tokens": 50, "stop": ["END"]}
    assert fill["tools"] == [{"name": "f", "description": "d", "parameters": {"type": "object"}}]


def test_scoping_and_roles(container, login, client, world, generation, make_project):
    member = login(client, world.users[Role.MEMBER])
    assert fetch(member, world, "nope").status_code == 404
    assert fetch(member, world, trace="other").status_code == 404
    other = make_project(world.org)
    r = member.get(f"/project/{other.id}/playground/from-observation/g1", params={"trace_id": "t1"})
    assert r.status_code == 404  # the generation belongs to another project
    viewer = login(client, world.users[Role.VIEWER])
    assert fetch(viewer, world).status_code == 403
