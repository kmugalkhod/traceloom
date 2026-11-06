import json
import logging

import httpx
import pytest

from traceloom_sdk import Traceloom, set_default_client


class Recorder:
    """An httpx MockTransport handler that records batches; `statuses` scripts the replies."""

    def __init__(self):
        self.requests = []
        self.statuses = []  # popped per request; empty means 207

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if self.statuses else 207
        if status == "connect-error":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(status, json={"successes": [], "errors": []})

    @property
    def events(self):
        out = []
        for request in self.requests:
            out.extend(json.loads(request.content)["batch"])
        return out

    def of_type(self, event_type):
        return [e for e in self.events if e["type"] == event_type]


@pytest.fixture
def recorder():
    return Recorder()


@pytest.fixture
def make_client(recorder):
    clients = []

    def make(**kwargs):
        kwargs.setdefault("host", "http://traceloom.test")
        kwargs.setdefault("public_key", "pk-test")
        kwargs.setdefault("secret_key", "sk-test")
        kwargs.setdefault("flush_interval", 60)
        kwargs.setdefault("backoff_base", 0.001)
        kwargs.setdefault("transport", httpx.MockTransport(recorder))
        client = Traceloom(**kwargs)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.shutdown(1)
    set_default_client(None)


@pytest.fixture
def client(make_client):
    c = make_client()
    set_default_client(c)
    return c


@pytest.fixture(autouse=True)
def _reset_default():
    set_default_client(None)
    yield
    set_default_client(None)


@pytest.fixture
def sdk_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="traceloom")
    return caplog


class Script:
    """Scripted replies for a provider SDK's HTTP client, whichever httpx generation it uses.

    (respx only patches `httpx`; recent provider SDKs ship their own `httpx2`.)
    """

    def __init__(self, module):
        self.module = module
        self.queue = []
        self.requests = []

    def reply(self, status=200, json=None, content=None, headers=None):
        self.queue.append({"status": status, "json": json, "content": content, "headers": headers})

    def _handler(self, request):
        self.requests.append(request)
        spec = self.queue.pop(0) if len(self.queue) > 0 else {"status": 500, "json": {}}
        kwargs = {"headers": spec.get("headers")}
        if spec.get("json") is not None:
            kwargs["json"] = spec["json"]
        if spec.get("content") is not None:
            kwargs["content"] = spec["content"]
        return self.module.Response(spec["status"], **kwargs)

    def http(self, is_async=False):
        transport = self.module.MockTransport(self._handler)
        return (self.module.AsyncClient if is_async else self.module.Client)(transport=transport)


def provider_script(sdk_module_name):
    import importlib

    base = importlib.import_module(f"{sdk_module_name}._base_client")
    return Script(getattr(base, "httpx2", None) or base.httpx)


@pytest.fixture
def openai_script():
    pytest.importorskip("openai")
    return provider_script("openai")


@pytest.fixture
def anthropic_script():
    pytest.importorskip("anthropic")
    return provider_script("anthropic")
