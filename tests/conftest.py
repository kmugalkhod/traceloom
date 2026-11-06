from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from traceloom.config import Settings
from traceloom.main import create_app

pytest_plugins = ["tests.factories", "tests.contract.stores"]

# Outside dev Settings requires an encryption key; tests share a fixed throwaway one.
TEST_ENCRYPTION_KEY = "dHJhY2Vsb29tLXRlc3Qta2V5LW5vdC1mb3ItcHJvZCE="
os.environ.setdefault("TRACELOOM_ENCRYPTION_KEY", TEST_ENCRYPTION_KEY)

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


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
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
