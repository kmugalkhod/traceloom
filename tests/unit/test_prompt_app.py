from __future__ import annotations

import json

import httpx
import pytest
from traceloom_sdk import Traceloom, set_default_client

from examples import prompt_app

PROMPT = {
    "name": "support",
    "type": "chat",
    "version": 2,
    "prompt": [{"role": "user", "content": "Hi {{name}}: {{question}}"}],
    "config": {},
    "labels": ["production"],
    "tags": [],
    "commitMessage": "m",
    "dependencies": [],
}


def make(prompt_status: int):
    events: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/prompts/" in request.url.path:
            return httpx.Response(prompt_status, json=PROMPT if prompt_status == 200 else {})
        events.extend(json.loads(request.content)["batch"])
        return httpx.Response(207, json={"successes": [], "errors": []})

    client = Traceloom(
        host="http://traceloom.test",
        public_key="pk",
        secret_key="sk",
        flush_interval=60,
        transport=httpx.MockTransport(handler),
    )
    set_default_client(client)
    return client, events


@pytest.fixture(autouse=True)
def _reset():
    yield
    set_default_client(None)


def test_fetched_prompt_is_compiled_and_linked():
    client, events = make(200)
    result = prompt_app.run("Ann", "refund?")
    client.flush()
    assert result["version"] == 2 and not result["is_fallback"]
    assert result["messages"] == [{"role": "user", "content": "Hi Ann: refund?"}]
    create = next(e["body"] for e in events if e["type"] == "generation-create")
    assert (create["promptName"], create["promptVersion"]) == ("support", 2)


def test_unreachable_server_uses_fallback_and_does_not_link():
    client, events = make(503)
    result = prompt_app.run("Ann", "refund?", "support")
    client.flush()
    assert result["is_fallback"] is True
    assert "helping Ann" in result["messages"][0]["content"]
    create = next(e["body"] for e in events if e["type"] == "generation-create")
    assert "promptName" not in create and "promptVersion" not in create
