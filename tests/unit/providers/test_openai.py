import json

import pytest

from tests.unit.providers.openai_helpers import (
    EXPECTED_MESSAGES,
    EXPECTED_TOOLS,
    MESSAGES,
    PARAMS,
    SCHEMA,
    TOOLS,
    stream_reply,
)
from traceloom.llm import providers
from traceloom.llm.providers import (
    DoneDelta,
    ErrorDelta,
    ProviderConfig,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
)
from traceloom.llm.providers.openai import OpenAIProvider

KEY = "sk-proj-supersecretvalue1234"


@pytest.fixture
def provider(openai_script):
    return OpenAIProvider(
        ProviderConfig(api_key=KEY, base_url="http://openai.test/v1"),
        http_client=openai_script.http(),
    )


def test_request_mapping(provider, openai_script):
    openai_script.reply(**stream_reply())
    list(provider.stream(MESSAGES, "gpt-4o", PARAMS, TOOLS, SCHEMA))
    request = openai_script.requests[-1]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "gpt-4o" and body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"] == EXPECTED_MESSAGES and body["tools"] == EXPECTED_TOOLS
    assert (body["max_completion_tokens"], body["temperature"], body["top_p"]) == (300, 0.3, 0.8)
    assert body["stop"] == ["END"]
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "schema": SCHEMA},
    }


def test_streams_text_tool_call_and_usage(provider, openai_script):
    openai_script.reply(**stream_reply())
    out = list(provider.stream([{"role": "user", "content": "hi"}], "gpt-4o"))
    assert out == [
        TextDelta("Hel"),
        TextDelta("lo"),
        ToolCallDelta("call_9", "weather", {"city": "Paris"}),
        UsageDelta(12, 6, {"cache_read": 4, "reasoning": 2}),
        DoneDelta("tool_calls"),
    ]


def test_errors_are_scrubbed(provider, openai_script):
    openai_script.reply(
        status=401,
        json={"error": {"message": f"Incorrect API key provided: {KEY}", "type": "invalid"}},
    )
    out = list(provider.stream([{"role": "user", "content": "hi"}], "gpt-4o"))
    assert len(out) == 1 and isinstance(out[0], ErrorDelta)
    assert "Incorrect API key" in out[0].message and "supersecret" not in out[0].message


def test_registered_by_name():
    assert providers.get_factory("openai") is OpenAIProvider
