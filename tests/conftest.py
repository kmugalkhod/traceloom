from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from traceloom.config import Settings
from traceloom.llm import providers
from traceloom.llm.providers import DoneDelta, TextDelta
from traceloom.main import create_app

pytest_plugins = ["tests.factories", "tests.contract.stores"]

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def pytest_collection_modifyitems(config, items) -> None:
    if os.environ.get("TRACELOOM_IT") == "1":
        return
    skip_it = pytest.mark.skip(reason="integration test: set TRACELOOM_IT=1")
    for item in items:
        if "it" in item.keywords:
            item.add_marker(skip_it)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite must pass offline: only loopback and unix sockets may connect."""
    real_connect = socket.socket.connect

    def guarded(self, address):
        if isinstance(address, tuple) and str(address[0]) not in _LOOPBACK:
            raise AssertionError(f"network access attempted in tests: {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


class FakeProvider:
    """Scripted provider: each `script(...)` call queues the Deltas (or an exception) for one call."""

    name = "fake"

    def __init__(self) -> None:
        self.scripts: list[list] = []
        self.calls: list[dict] = []

    def script(self, *deltas) -> None:
        self.scripts.append(list(deltas))

    def __call__(self, config) -> FakeProvider:  # the registry factory
        self.config = config
        return self

    def stream(self, messages, model, params=None, tools=None, response_schema=None):
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "params": dict(params or {}),
                "tools": list(tools or []),
                "response_schema": response_schema,
            }
        )
        for item in self.scripts.pop(0) if self.scripts else [TextDelta("ok"), DoneDelta("stop")]:
            if isinstance(item, BaseException):
                raise item
            yield item


@pytest.fixture
def fake_provider() -> Iterator[FakeProvider]:
    """A scripted adapter registered under the provider name "fake"."""
    fake = FakeProvider()
    providers.register("fake", fake)
    yield fake
    providers.unregister("fake")


@pytest.fixture
def encryption_key() -> str:
    from traceloom.llm.crypto import generate_key

    return generate_key()


@pytest.fixture
def settings(tmp_path: Path, encryption_key: str) -> Settings:
    return Settings(
        TRACELOOM_ENCRYPTION_KEY=encryption_key,
        TRACELOOM_ENV="test",
        TRACELOOM_MODE="lite",
        DATABASE_URL=f"sqlite:///{tmp_path / 'test.db'}",
        TRACELOOM_BLOB_DIR=str(tmp_path / "blobs"),
        TRACELOOM_SERVER_SECRET="test-secret-key-not-for-production",
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:  # runs the lifespan (container startup/shutdown)
        yield c
