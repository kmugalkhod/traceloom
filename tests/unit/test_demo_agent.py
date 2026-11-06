from __future__ import annotations

import json

import httpx
import pytest
import traceloom_sdk
from traceloom_sdk import Traceloom, set_default_client

from examples import agent


@pytest.fixture
def sent():
    batches: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        batches.append(json.loads(request.content))
        return httpx.Response(207, json={"successes": [], "errors": []})

    client = Traceloom(
        host="http://traceloom.test",
        public_key="pk",
        secret_key="sk",
        flush_interval=60,
        transport=httpx.MockTransport(handler),
    )
    set_default_client(client)
    yield batches
    client.shutdown(1)
    set_default_client(None)


def events(batches):
    return [e for b in batches for e in b["batch"]]


def test_agent_runs_offline_and_sends_a_three_level_tree_with_one_tool_error(sent):
    result = agent.run()
    assert traceloom_sdk.flush() is True
    assert "escalated" in result["answer"]

    creates = {
        e["body"]["id"]: e["body"]
        for e in events(sent)
        if e["type"].endswith("-create") and e["type"] != "trace-create"
    }
    assert {e["body"]["traceId"] for e in events(sent) if "traceId" in e["body"]} == {
        result["trace_id"]
    }

    def depth(obs_id: str) -> int:
        parent = creates[obs_id].get("parentObservationId")
        return 0 if parent is None else 1 + depth(parent)

    assert max(depth(i) for i in creates) >= 2  # agent -> step -> generation/tool
    types = {c["name"]: c["type"] for c in creates.values()}
    assert types["support-agent"] == "agent" and types["lookup_order"] == "tool"
    assert types["search"] == "tool" and types["calculator"] == "tool"

    errors = [
        e["body"]
        for e in events(sent)
        if e["type"].endswith("-update") and e["body"].get("level") == "ERROR"
    ]
    assert len(errors) == 1
    assert creates[errors[0]["id"]]["name"] == "lookup_order"
    assert "orders service unavailable" in errors[0]["statusMessage"]

    generations = [e["body"] for e in events(sent) if e["type"] == "generation-create"]
    updates = {e["body"]["id"]: e["body"] for e in events(sent) if e["type"] == "generation-update"}
    assert len(generations) == 4
    for gen in generations:
        up = updates[gen["id"]]
        assert gen["model"].startswith("claude-") and up["usageDetails"]["total"] > 0
        assert up["completionStartTime"] >= gen["startTime"]


def test_main_exits_zero_offline_even_with_tracing_disabled(monkeypatch, capsys):
    for name in ("TRACELOOM_PUBLIC_KEY", "TRACELOOM_SECRET_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    set_default_client(None)
    try:
        assert agent.main([]) == 0
    finally:
        set_default_client(None)
    captured = capsys.readouterr()
    assert "refund" in captured.out and "Tracing is off" in captured.err


def test_calculator_rejects_unsafe_expressions():
    assert agent.calculator.__wrapped__("49.99 * 2") == "99.98"
    with pytest.raises(ValueError):
        agent.calculator.__wrapped__("__import__('os').system('x')")
