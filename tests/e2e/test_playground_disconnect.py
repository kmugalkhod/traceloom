"""A client that goes away mid-stream must stop the provider (real uvicorn, real socket).

ASGI test clients fake `http.disconnect`; under uvicorn a sync generator only learns about a
closed socket from a failed write, so this runs the real server.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from urllib.parse import urlsplit

from sqlalchemy import select

from traceloom.auth.csrf import COOKIE_NAME as CSRF_COOKIE
from traceloom.auth.csrf import issue_token
from traceloom.auth.permissions import Role
from traceloom.auth.sessions import COOKIE_NAME as SESSION_COOKIE
from traceloom.auth.sessions import issue_session
from traceloom.db.models import Project, User
from traceloom.llm import connections, providers
from traceloom.llm.connections import ConnectionPolicy
from traceloom.llm.providers import BaseProvider, DoneDelta, TextDelta
from traceloom.prompts.service import Actor

CHUNKS = 60
PAUSE = 0.05


class Slow(BaseProvider):
    produced = 0
    closed = threading.Event()
    finished = threading.Event()

    def _stream(self, *args):
        try:
            for i in range(CHUNKS):
                Slow.produced += 1
                yield TextDelta(f"chunk-{i} ")
                time.sleep(PAUSE)
            Slow.finished.set()
            yield DoneDelta("stop")
        finally:
            Slow.closed.set()


def test_closing_the_socket_stops_the_provider(live):
    Slow.produced = 0
    Slow.closed.clear()
    Slow.finished.clear()
    providers.register("anthropic", Slow)
    try:
        container = live.container
        secret = container.settings.secret_key
        with container.session_factory() as db:
            user = db.scalar(select(User).where(User.email == live.account.email))
            project = db.get(Project, live.account.project_id)
            actor = Actor.for_user(user, Role.OWNER)
            conn = connections.create(
                db, project, actor, ConnectionPolicy.from_settings(container.settings),
                provider="anthropic", name="slow", secrets={"api_key": "sk-ant-live-0000111122"},
            )  # fmt: skip
            db.commit()
            session_cookie = issue_session(container.settings, user.id, user.session_version)
            conn_id = conn.id
        token = issue_token(secret)
        payload = json.dumps(
            {
                "connection_id": conn_id,
                "model": "claude-haiku-4-5-20251001",
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode()
        url = urlsplit(live.url)
        path = f"/project/{live.account.project_id}/playground/run"
        request = (
            f"POST {path} HTTP/1.1\r\nHost: {url.netloc}\r\nAccept: application/json\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
            f"Cookie: {SESSION_COOKIE}={session_cookie}; {CSRF_COOKIE}={token}\r\n"
            f"X-CSRF-Token: {token}\r\n\r\n"
        ).encode() + payload
        sock = socket.create_connection((url.hostname, url.port), timeout=10)
        sock.sendall(request)
        seen = b""
        while seen.count(b"event: delta") < 4:
            data = sock.recv(4096)
            assert data, f"server closed early: {seen!r}"
            seen += data
        pass
        sock.close()
        assert Slow.closed.wait(5), "the provider stream was never closed"
        assert not Slow.finished.is_set() and Slow.produced < CHUNKS
    finally:
        providers.unregister("anthropic")
