"""SSRF for LLM connections: every outgoing provider request is checked, pinned and unredirected."""

from __future__ import annotations

import pytest

from tests.unit.providers.openai_helpers import stream_reply
from tests.unit.providers.test_anthropic import events as anthropic_events
from tests.unit.providers.test_google import chunk as google_chunk
from tests.unit.providers.test_google import sse_reply as google_reply
from traceloom.llm import http
from traceloom.llm.providers import ErrorDelta, ProviderConfig, TextDelta, create
from traceloom.llm.ssrf import BlockedAddress

KEY = "sk-proj-pinnedtestvalue1234"
PUBLIC = "93.184.216.34"
METADATA = "169.254.169.254"
HI = [{"role": "user", "content": "hi"}]


def flipping(*answers):
    """A resolver that gives each answer once, in order (then repeats the last)."""
    queue = list(answers)

    def resolve(host, port):
        return [queue.pop(0) if len(queue) > 1 else queue[0]]

    return resolve


class Inner:
    """A recording inner transport of the right httpx generation."""

    def __init__(self, module, replies):
        self.module = module
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        spec = self.replies.pop(0) if self.replies else {"status": 500}
        return self.module.Response(
            spec.get("status", 200),
            headers=spec.get("headers"),
            content=spec.get("content"),
            json=spec.get("json"),
        )

    def transport(self):
        return self.module.MockTransport(self)


@pytest.fixture
def capture(monkeypatch):
    """Route the adapters' own pinned clients to a recording inner transport."""
    seen: dict[str, Inner] = {}
    real = http.pinned_client

    def install(*replies):
        def pinned_client(module, **kwargs):
            inner = seen.setdefault("inner", Inner(module, replies))
            return real(module, **kwargs, inner=inner.transport())

        monkeypatch.setattr(http, "pinned_client", pinned_client)
        return seen

    return install


# --- the client ---------------------------------------------------------------------------------


@pytest.mark.parametrize("sdk", ["openai", "anthropic", None])
def test_connects_to_the_checked_address_with_the_original_host_and_sni(sdk):
    module = http.httpx_module(sdk)
    inner = Inner(module, [{"status": 200, "json": {}}])
    client = http.pinned_client(
        module,
        allow_private=False,
        timeout=5,
        resolver=lambda h, p: [PUBLIC],
        inner=inner.transport(),
    )
    assert client.get("https://api.example.test/v1/models?x=1").status_code == 200
    sent = inner.requests[0]
    assert (sent.url.host, sent.url.path, sent.url.query) == (PUBLIC, "/v1/models", b"x=1")
    assert sent.headers["host"] == "api.example.test"
    assert sent.extensions["sni_hostname"] == "api.example.test"


def test_dns_rebinding_between_check_and_connect_is_blocked():
    module = http.httpx_module("openai")
    inner = Inner(module, [{"status": 200}])
    client = http.pinned_client(
        module,
        allow_private=True,
        timeout=5,
        resolver=flipping(PUBLIC, METADATA),
        inner=inner.transport(),
    )
    assert client.get("https://api.example.test/").status_code == 200  # check 1: public
    with pytest.raises(BlockedAddress):
        client.get("https://api.example.test/")  # the next answer is the metadata address
    assert len(inner.requests) == 1


def test_a_redirect_to_the_metadata_address_is_not_followed():
    module = http.httpx_module("openai")
    location = f"http://{METADATA}/latest/meta-data/"
    inner = Inner(module, [{"status": 302, "headers": {"location": location}}, {"status": 200}])
    client = http.pinned_client(
        module,
        allow_private=True,
        timeout=5,
        resolver=lambda h, p: [PUBLIC],
        inner=inner.transport(),
    )
    response = client.get("https://api.example.test/")
    assert response.status_code == 302 and len(inner.requests) == 1
    with pytest.raises(BlockedAddress):  # even a caller that follows it by hand is stopped
        client.get(location)
    assert len(inner.requests) == 1


# --- the adapters use it --------------------------------------------------------------------------


def _cfg(base_url, resolver, config=None):
    return ProviderConfig(
        api_key=KEY,
        base_url=base_url,
        allow_private=True,
        resolver=resolver,
        config=config or {},
    )


OPENAI_FAMILY = [
    ("openai", "https://openai.example.test/v1", {}),
    ("azure_openai", "https://res.example.test", {"api_version": "2024-10-21"}),
    ("openai_compatible", "https://gateway.example.test/v1", {}),
]


@pytest.mark.parametrize(("name", "base_url", "config"), OPENAI_FAMILY)
def test_openai_family_streams_through_the_pinned_client(capture, name, base_url, config):
    seen = capture(stream_reply())
    provider = create(name, _cfg(base_url, lambda h, p: [PUBLIC], config))
    out = list(provider.stream(HI, "gpt-4o"))
    assert out[0] == TextDelta("Hel"), out
    sent = seen["inner"].requests[0]
    assert sent.url.host == PUBLIC and sent.headers["host"] == base_url.split("/")[2]


def test_anthropic_streams_through_the_pinned_client(capture):
    seen = capture(anthropic_events(("text", ["ok"])))
    provider = create("anthropic", _cfg("https://anthropic.example.test", lambda h, p: [PUBLIC]))
    out = list(provider.stream(HI, "claude-haiku-4-5"))
    assert TextDelta("ok") in out, out
    assert seen["inner"].requests[0].headers["host"] == "anthropic.example.test"


def test_google_streams_through_the_pinned_client(capture):
    seen = capture(google_reply(google_chunk([{"text": "ok"}], finish="STOP")))
    provider = create("google", _cfg("https://gemini.example.test", lambda h, p: [PUBLIC]))
    out = list(provider.stream(HI, "gemini-2.5-flash"))
    assert TextDelta("ok") in out, out
    assert seen["inner"].requests[0].headers["host"] == "gemini.example.test"


@pytest.mark.parametrize(
    ("name", "base_url", "config"),
    [
        *OPENAI_FAMILY,
        ("anthropic", "https://anthropic.example.test", {}),
        ("google", "https://gemini.example.test", {}),
    ],
)
def test_rebinding_to_metadata_after_the_earlier_checks_is_blocked(name, base_url, config):
    """The base URL resolved to a public address when it was checked; then DNS flips."""
    provider = create(name, _cfg(base_url, flipping(PUBLIC, PUBLIC, METADATA), config))
    resolver = provider.cfg.resolver
    while resolver("x", 443) != [METADATA]:  # use up the answers the earlier checks would get
        pass
    out = list(provider.stream(HI, "m"))
    errors = [d for d in out if isinstance(d, ErrorDelta)]
    assert errors and "not allowed" in errors[0].message, out


def test_bedrock_refuses_a_metadata_endpoint():
    cfg = ProviderConfig(
        api_key=KEY,
        base_url=f"http://{METADATA}/",
        config={"region": "us-east-1"},
        secrets={"access_key_id": "AKIAEXAMPLEEXAMPLE"},
        allow_private=True,
    )
    out = list(create("bedrock", cfg).stream(HI, "anthropic.claude"))
    assert len(out) == 1 and isinstance(out[0], ErrorDelta) and "not allowed" in out[0].message


# --- clients are always closed (review fix) -------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "base_url", "config"),
    [
        *OPENAI_FAMILY,
        ("anthropic", "https://anthropic.example.test", {}),
        ("google", "https://gemini.example.test", {}),
    ],
)
def test_the_http_client_is_closed_when_the_request_fails(monkeypatch, name, base_url, config):
    built = []
    real = http.pinned_client

    def pinned_client(module, **kwargs):
        inner = Inner(module, [{"status": 500, "json": {"error": {"message": "boom"}}}])
        client = real(module, **kwargs, inner=inner.transport())
        built.append(client)
        return client

    monkeypatch.setattr(http, "pinned_client", pinned_client)
    provider = create(name, _cfg(base_url, lambda h, p: [PUBLIC], config))
    out = list(provider.stream(HI, "m"))
    assert isinstance(out[-1], ErrorDelta)
    assert built and all(c.is_closed for c in built)


def test_bedrock_client_is_closed_when_the_call_fails(monkeypatch):
    import boto3

    class Client:
        closed = False

        def converse_stream(self, **request):
            raise RuntimeError("throttled")

        def close(self):
            Client.closed = True

    monkeypatch.setattr(boto3, "client", lambda *a, **k: Client())
    cfg = ProviderConfig(
        api_key=KEY, config={"region": "us-east-1"}, secrets={"access_key_id": "AKIAEXAMPLE1"}
    )
    out = list(create("bedrock", cfg).stream(HI, "anthropic.claude"))
    assert isinstance(out[-1], ErrorDelta) and Client.closed
