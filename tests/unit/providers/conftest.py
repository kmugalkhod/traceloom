"""Fake HTTP transports for the provider SDKs (recent ones ship their own `httpx2`)."""

from __future__ import annotations

import importlib
import json

import pytest


class Script:
    """Scripted replies for a provider SDK's HTTP client, whichever httpx generation it uses."""

    def __init__(self, module):
        self.module = module
        self.queue = []
        self.requests = []

    def reply(self, status=200, json=None, content=None, headers=None):
        self.queue.append({"status": status, "json": json, "content": content, "headers": headers})

    def _handler(self, request):
        self.requests.append(request)
        spec = self.queue.pop(0) if self.queue else {"status": 500, "json": {}}
        kwargs = {"headers": spec.get("headers")}
        if spec.get("json") is not None:
            kwargs["json"] = spec["json"]
        if spec.get("content") is not None:
            kwargs["content"] = spec["content"]
        return self.module.Response(spec["status"], **kwargs)

    def http(self):
        return self.module.Client(transport=self.module.MockTransport(self._handler))

    @property
    def body(self):
        return json.loads(self.requests[-1].content)


def sse(events):
    """A text/event-stream reply from (name, data) pairs."""
    body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
    return {"content": body, "headers": {"content-type": "text/event-stream"}}


def _script(sdk):
    pytest.importorskip(sdk)
    base = importlib.import_module(f"{sdk}._base_client")
    return Script(getattr(base, "httpx2", None) or base.httpx)


@pytest.fixture
def anthropic_script():
    return _script("anthropic")


@pytest.fixture
def openai_script():
    return _script("openai")
