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
