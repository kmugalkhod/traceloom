import json
from urllib.parse import quote

import boto3
import pytest
from botocore.awsrequest import AWSResponse
from botocore.stub import Stubber

from tests.unit.providers.openai_helpers import CITY, MESSAGES, SCHEMA, TOOLS
from traceloom.llm import providers
from traceloom.llm.providers import (
    DoneDelta,
    ErrorDelta,
    ProviderConfig,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
)
from traceloom.llm.providers.bedrock import BedrockProvider
from traceloom.llm.ssrf import BlockedAddress

SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
KEY_ID = "AKIAIOSFODNN7EXAMPLE"
MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"


class Fake:
    """Answers ConverseStream from a before-call hook (Stubber cannot return an event stream),
    after botocore has validated the request against the service model."""

    def __init__(self, client, stream):
        self.calls = []
        self.stream = stream
        client.meta.events.register("before-call.bedrock-runtime.ConverseStream", self.hook)

    def hook(self, params, model, **kwargs):
        self.calls.append(params)
        return AWSResponse(None, 200, {}, None), {"stream": self.stream}


def make():
    client = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id=KEY_ID,
        aws_secret_access_key=SECRET,
    )
    cfg = ProviderConfig(api_key=SECRET, secrets={"access_key_id": KEY_ID})
    return BedrockProvider(cfg, client=client), Stubber(client)


EVENTS = [
    {"messageStart": {"role": "assistant"}},
    {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Hel"}}},
    {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "lo"}}},
    {"contentBlockStop": {"contentBlockIndex": 0}},
    {"contentBlockStart": {"contentBlockIndex": 1,
                           "start": {"toolUse": {"toolUseId": "tu_1", "name": "weather"}}}},
    {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": '{"city":'}}}},
    {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": ' "Paris"}'}}}},
    {"contentBlockStop": {"contentBlockIndex": 1}},
    {"messageStop": {"stopReason": "tool_use"}},
    {"metadata": {"usage": {"inputTokens": 12, "outputTokens": 6, "totalTokens": 18,
                            "cacheReadInputTokens": 4},
                  "metrics": {"latencyMs": 5}}},
]  # fmt: skip


def test_request_mapping_and_stream():
    provider, stub = make()
    expected = {
        "modelId": MODEL,
        "messages": [
            {"role": "user", "content": [{"text": "weather in Paris?"}]},
            {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": "call_1", "name": "weather",
                             "input": {"city": "Paris"}}}]},
            {"role": "user", "content": [
                {"toolResult": {"toolUseId": "call_1", "content": [{"text": "sunny"}]}}]},
        ],
        "system": [{"text": "Be brief."}],
        "inferenceConfig": {"maxTokens": 300, "temperature": 0.3, "topP": 0.8,
                            "stopSequences": ["END"]},
        "toolConfig": {"tools": [{"toolSpec": {"name": "weather", "description": "Get weather",
                                               "inputSchema": {"json": CITY}}}]},
        "outputConfig": {"textFormat": {"type": "json_schema", "structure": {
            "jsonSchema": {"name": "response", "schema": '{"type": "object", "properties": '
                           '{"a": {"type": "string"}}}'}}}},
    }  # fmt: skip
    params = {"max_tokens": 300, "temperature": 0.3, "top_p": 0.8, "stop": ["END"]}
    fake = Fake(provider._client(), EVENTS)
    out = list(provider.stream(MESSAGES, MODEL, params, TOOLS, SCHEMA))
    assert json.loads(fake.calls[0]["body"]) == {
        k: v for k, v in expected.items() if k != "modelId"
    }
    assert fake.calls[0]["url"].endswith(f"/model/{quote(MODEL, safe='')}/converse-stream")
    assert out == [
        TextDelta("Hel"),
        TextDelta("lo"),
        ToolCallDelta("tu_1", "weather", {"city": "Paris"}),
        UsageDelta(12, 6, {"cache_read": 4}),
        DoneDelta("tool_use"),
    ]


def test_errors_never_contain_credentials():
    provider, stub = make()
    stub.add_client_error(
        "converse_stream",
        service_error_code="AccessDeniedException",
        service_message=f"User {KEY_ID} with secret {SECRET} is not authorized",
        http_status_code=403,
    )
    with stub:
        out = list(provider.stream([{"role": "user", "content": "hi"}], MODEL))
    assert len(out) == 1 and isinstance(out[0], ErrorDelta)
    assert "AccessDeniedException" in out[0].message and "not authorized" in out[0].message
    assert KEY_ID not in out[0].message and SECRET not in out[0].message


def test_stream_failure_midway_is_scrubbed():
    provider, stub = make()
    events = [{"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "a"}}}]

    class Broken:
        def __iter__(self):
            yield from events
            raise RuntimeError(f"connection reset for {KEY_ID}")

    Fake(provider._client(), Broken())
    out = list(provider.stream([{"role": "user", "content": "hi"}], MODEL))
    assert out[0] == TextDelta("a") and isinstance(out[1], ErrorDelta)
    assert KEY_ID not in out[1].message


def test_registered_by_name():
    assert providers.get_factory("bedrock") is BedrockProvider


def test_closing_the_delta_stream_closes_the_event_stream():
    class Closable:
        closed = False

        def __iter__(self):
            return iter(EVENTS)

        def close(self):
            Closable.closed = True

    provider, _ = make()
    Fake(provider._client(), Closable())
    deltas = provider.stream([{"role": "user", "content": "hi"}], MODEL)
    assert next(deltas) == TextDelta("Hel")
    deltas.close()  # the caller (a cancelled playground run) walks away mid-stream
    assert Closable.closed


# --- custom endpoints are AWS-only and never proxied (final review) -----------------------------

PUBLIC = "52.94.0.10"
VPC = "https://vpce-0a1b2c3d4e5f-abcdefgh.bedrock-runtime.us-east-1.vpce.amazonaws.com"
REGIONAL = "https://bedrock-runtime.eu-central-1.amazonaws.com"


def endpoint_provider(url):
    cfg = ProviderConfig(
        api_key=SECRET,
        base_url=url,
        config={"region": "us-east-1"},
        secrets={"access_key_id": KEY_ID},
        resolver=lambda host, port: [PUBLIC],
    )
    return BedrockProvider(cfg)


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example.com",
        "https://bedrock-runtime.us-east-1.amazonaws.com.attacker.example",
        "https://evilamazonaws.com",
        "http://bedrock-runtime.us-east-1.amazonaws.com",
    ],
)
def test_a_non_aws_or_plain_http_endpoint_is_refused_at_build(url):
    with pytest.raises(BlockedAddress, match="AWS"):
        endpoint_provider(url)._client()
    out = list(endpoint_provider(url).stream([{"role": "user", "content": "hi"}], MODEL))
    assert len(out) == 1 and isinstance(out[0], ErrorDelta) and "not allowed" in out[0].message


@pytest.mark.parametrize(
    "url", [VPC, REGIONAL, "https://bedrock-runtime.cn-north-1.amazonaws.com.cn"]
)
def test_vpc_and_regional_endpoints_are_accepted(url):
    client = endpoint_provider(url)._client()
    assert client.meta.endpoint_url == url
    client.close()


def test_environment_proxies_are_ignored(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.attacker.example:3128")
    monkeypatch.setenv("https_proxy", "http://proxy.attacker.example:3128")
    built = {}
    real = boto3.client

    def client(*args, **kwargs):
        built.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(boto3, "client", client)
    made = endpoint_provider(REGIONAL)._client()
    assert built["config"].proxies == {}
    assert made._endpoint.http_session._proxy_config.proxy_url_for(REGIONAL + "/") is None
    made.close()
