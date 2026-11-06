import asyncio
import threading
import time

import httpx
import pytest

import traceloom_sdk
from traceloom_sdk import ChatPrompt, PromptNotFound, TextPrompt, Traceloom
from traceloom_sdk.prompts import PromptCache

TEXT = {
    "name": "support",
    "type": "text",
    "version": 1,
    "prompt": "Hello {{name}}, about {{topic}}",
    "config": {"model": "m"},
    "labels": ["production", "latest"],
    "tags": ["cs"],
    "commitMessage": "init",
    "dependencies": [],
}
CHAT = {
    **TEXT,
    "type": "chat",
    "prompt": [
        {"role": "system", "content": "You help {{name}}."},
        {"role": "placeholder", "content": "history"},
        {"role": "user", "content": "{{question}}"},
    ],
}


class Server:
    """A scripted prompt endpoint recording every request."""

    def __init__(self, body=TEXT):
        self.body = body
        self.requests: list[httpx.Request] = []
        self.script: list = []  # popped per request: a status int, "error", or None for 200
        self.delay = 0.0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            time.sleep(self.delay)
        step = self.script.pop(0) if self.script else None
        if step == "error":
            raise httpx.ConnectError("refused", request=request)
        if isinstance(step, int):
            return httpx.Response(step, json={"error": {"code": "x", "message": "m"}})
        return httpx.Response(200, json=self.body)


def make(server, **kw):
    return Traceloom(
        host="http://t",
        public_key="pk",
        secret_key="sk",
        transport=httpx.MockTransport(server),
        **kw,
    )


def join_refreshers(client, timeout=2):
    for thread in list(client._prompt_cache.refreshers.values()):
        thread.join(timeout)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("TRACELOOM_PROMPT_CACHE_TTL", raising=False)


def test_text_compile_and_attributes():
    client = make(Server())
    prompt = client.get_prompt("support")
    assert isinstance(prompt, TextPrompt)
    assert prompt.compile(name="Ann") == "Hello Ann, about {{topic}}"  # unknown left as-is
    assert prompt.compile(name="Ann", topic="refunds", extra=1) == "Hello Ann, about refunds"
    assert (prompt.name, prompt.version, prompt.config) == ("support", 1, {"model": "m"})
    assert prompt.labels == ["production", "latest"] and prompt.variables == ["name", "topic"]
    assert prompt.is_fallback is False
    assert prompt.prompt == TEXT["prompt"]  # compile never mutates


def test_chat_compile():
    prompt = make(Server(CHAT)).get_prompt("support")
    assert isinstance(prompt, ChatPrompt)
    out = prompt.compile(name="Ann", question="Why?")
    assert [m["content"] for m in out] == ["You help Ann.", "history", "Why?"]
    assert prompt.prompt[0]["content"] == "You help {{name}}."
    assert prompt.variables == ["name", "question"]


def test_request_shape_and_encoding():
    server = Server()
    client = make(server)
    client.get_prompt("support/tone", label="staging")
    client.get_prompt("support/tone", version=3)
    client.get_prompt("plain")
    first, second, third = server.requests
    assert first.url.raw_path.startswith(b"/api/public/v2/prompts/support%2Ftone?label=staging")
    assert second.url.params["version"] == "3" and "label" not in second.url.params
    assert not third.url.params
    assert first.headers["authorization"].startswith("Basic ")


def test_warm_cache_makes_no_network_call():
    server = Server()
    client = make(server)
    for _ in range(5):
        client.get_prompt("support")
    assert len(server.requests) == 1
    client.get_prompt("support", label="staging")  # a different key
    assert len(server.requests) == 2


def test_ttl_from_env_and_zero_disables(monkeypatch):
    monkeypatch.setenv("TRACELOOM_PROMPT_CACHE_TTL", "0")
    server = Server()
    client = make(server)
    client.get_prompt("support")
    client.get_prompt("support")
    assert len(server.requests) == 2
    assert not client._prompt_cache.refreshers


def test_stale_while_revalidate_with_one_second_ttl():
    server = Server()
    client = make(server)
    assert client.get_prompt("support", cache_ttl_seconds=1).version == 1
    server.body = {**TEXT, "version": 2, "prompt": "new {{name}}"}
    time.sleep(1.1)
    started = time.monotonic()
    stale = client.get_prompt("support", cache_ttl_seconds=1)
    assert stale.version == 1  # old version, straight away
    assert time.monotonic() - started < 0.5
    join_refreshers(client)
    fresh = client.get_prompt("support", cache_ttl_seconds=1)
    assert fresh.version == 2 and fresh.compile(name="Ann") == "new Ann"


def test_stale_value_returned_without_waiting_for_a_slow_refresh():
    server = Server()
    client = make(server)
    client.get_prompt("support", cache_ttl_seconds=0.05)
    time.sleep(0.1)
    server.delay = 0.5
    started = time.monotonic()
    assert client.get_prompt("support", cache_ttl_seconds=0.05).version == 1
    assert time.monotonic() - started < 0.3
    join_refreshers(client)


def test_one_refresher_per_key_and_it_is_a_daemon():
    server = Server()
    client = make(server)
    client.get_prompt("support", cache_ttl_seconds=0.05)
    time.sleep(0.1)
    server.delay = 0.3
    before = len(server.requests)
    for _ in range(10):
        client.get_prompt("support", cache_ttl_seconds=0.05)
    (thread,) = client._prompt_cache.refreshers.values()
    assert thread.daemon
    join_refreshers(client)
    assert len(server.requests) == before + 1
    assert client._prompt_cache.refreshers == {}


def test_failed_refresh_keeps_serving_the_stale_value_and_never_raises():
    server = Server()
    client = make(server, max_retries=0)
    client.get_prompt("support", cache_ttl_seconds=0.05, max_retries=0)
    time.sleep(0.1)
    server.script = [500]
    assert client.get_prompt("support", cache_ttl_seconds=0.05, max_retries=0).version == 1
    join_refreshers(client)
    assert client._prompt_cache.refreshers == {}
    server.script = ["error"]
    time.sleep(0.1)
    assert client.get_prompt("support", cache_ttl_seconds=0.05, max_retries=0).version == 1
    join_refreshers(client)


def test_cold_failure_uses_fallback():
    server = Server()
    server.script = ["error"] * 3
    client = make(server)
    prompt = client.get_prompt("support", fallback="Hi {{name}}", max_retries=2)
    assert isinstance(prompt, TextPrompt) and prompt.is_fallback is True
    assert prompt.compile(name="Ann") == "Hi Ann" and prompt.version is None
    assert len(server.requests) == 3
    # fallbacks are not cached: the next call tries the server again and recovers
    assert client.get_prompt("support", fallback="Hi").is_fallback is False


def test_chat_fallback():
    server = Server()
    server.script = [404]
    client = make(server)
    msgs = [{"role": "user", "content": "{{q}}"}]
    prompt = client.get_prompt("x", fallback=msgs)
    assert isinstance(prompt, ChatPrompt) and prompt.is_fallback
    assert prompt.compile(q="hi") == [{"role": "user", "content": "hi"}]
    server.script = [404]
    forced = client.get_prompt("y", fallback="plain", type="text")
    assert isinstance(forced, TextPrompt)


def test_cold_failure_without_fallback_raises():
    server = Server()
    server.script = [404]
    with pytest.raises(PromptNotFound):
        make(server).get_prompt("ghost")
    assert len(server.requests) == 1  # a 404 is not retried


def test_retries_on_5xx_and_network_errors_then_succeeds():
    server = Server()
    server.script = [503, "error"]
    assert make(server).get_prompt("support", max_retries=2).version == 1
    assert len(server.requests) == 3


def test_client_errors_are_not_retried():
    server = Server()
    server.script = [401]
    with pytest.raises(PromptNotFound):
        make(server).get_prompt("support", max_retries=3)
    assert len(server.requests) == 1


def test_cold_miss_is_bounded_by_timeout_times_attempts():
    server = Server()
    server.delay = 0.3
    server.script = [503] * 10
    client = make(server)
    started = time.monotonic()
    with pytest.raises(PromptNotFound):
        client.get_prompt("support", max_retries=1, fetch_timeout_seconds=0.2)
    assert time.monotonic() - started < 0.2 * 2 + 0.5


def test_no_credentials_means_no_network_call():
    server = Server()
    client = Traceloom(host="http://t", transport=httpx.MockTransport(server))
    client.public_key = client.secret_key = None
    assert client.get_prompt("support", fallback="fb").is_fallback
    with pytest.raises(PromptNotFound):
        client.get_prompt("support")
    assert server.requests == []


def test_malformed_response_is_a_failure_not_a_crash():
    server = Server(body={"unexpected": True})
    assert make(server).get_prompt("support", fallback="fb").is_fallback


def test_cache_is_bounded():
    cache = PromptCache(max_entries=3)
    for i in range(5):
        cache.put(i, i)
    assert cache.get(0, 10) is None and cache.get(4, 10) == (4, True)


def test_aget_prompt_and_module_functions():
    server = Server()
    client = make(server)
    prompt = asyncio.run(client.aget_prompt("support"))
    assert prompt.version == 1
    traceloom_sdk.set_default_client(client)
    try:
        assert traceloom_sdk.get_prompt("support").compile(name="A").startswith("Hello A")
        assert asyncio.run(traceloom_sdk.aget_prompt("support")).version == 1
        assert len(server.requests) == 1  # all served from the one cache
    finally:
        traceloom_sdk.set_default_client(None)


def test_threads_share_one_fetch_result_safely():
    server = Server()
    client = make(server)
    results = []

    def worker():
        results.append(client.get_prompt("support").version)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [1] * 8
