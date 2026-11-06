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


# --- the same, through a real OpenAI SDK connection to a real HTTP provider -------------------


class FakeOpenAI:
    """An OpenAI-compatible server that streams CHUNKS chunks and notices a dropped connection."""

    def __init__(self):
        import json as _json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        outer = self
        self.sent = 0
        self.disconnected = threading.Event()
        self.finished = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["content-length"]))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()

                def send(obj):
                    text = obj if isinstance(obj, str) else _json.dumps(obj)
                    data = f"data: {text}\n\n".encode()
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()

                try:
                    for i in range(CHUNKS):
                        send({"id": "c", "object": "chat.completion.chunk", "model": "m",
                              "choices": [{"index": 0, "delta": {"content": f"w{i} "},
                                           "finish_reason": None}]})  # fmt: skip
                        outer.sent += 1
                        time.sleep(PAUSE)
                    send("[DONE]")
                    self.wfile.write(b"0\r\n\r\n")
                    outer.finished.set()
                except (BrokenPipeError, ConnectionResetError):
                    outer.disconnected.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()


def test_closing_the_socket_closes_the_upstream_provider_connection(live):
    fake = FakeOpenAI()
    try:
        container = live.container
        with container.session_factory() as db:
            user = db.scalar(select(User).where(User.email == live.account.email))
            project = db.get(Project, live.account.project_id)
            conn = connections.create(
                db, project, Actor.for_user(user, Role.OWNER),
                ConnectionPolicy.from_settings(container.settings),
                provider="openai_compatible", name="fake-openai", base_url=fake.url,
                secrets={"api_key": "sk-live-openai-00001111"},
            )  # fmt: skip
            db.commit()
            session_cookie = issue_session(container.settings, user.id, user.session_version)
            conn_id = conn.id
        token = issue_token(container.settings.secret_key)
        payload = json.dumps(
            {"connection_id": conn_id, "model": "m", "messages": [{"role": "user", "content": "x"}]}
        ).encode()
        url = urlsplit(live.url)
        request = (
            f"POST /project/{live.account.project_id}/playground/run HTTP/1.1\r\n"
            f"Host: {url.netloc}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
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
        sock.close()
        assert fake.disconnected.wait(3), f"the provider connection stayed open ({fake.sent} sent)"
        assert not fake.finished.is_set() and fake.sent < CHUNKS
    finally:
        fake.stop()
