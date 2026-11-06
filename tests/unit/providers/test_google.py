import json

import pytest

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
from traceloom.llm.providers.google import GoogleProvider

KEY = "AIzaSyA-supersecretkey-0123456789"


def chunk(parts=None, finish=None, usage=None):
    out = {}
    if parts is not None or finish is not None:
        out["candidates"] = [
            {
                "content": {"role": "model", "parts": parts or []},
                **({"finishReason": finish} if finish else {}),
            }
        ]
    if usage:
        out["usageMetadata"] = usage
    return out


def sse_reply(*chunks):
    body = "".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks)
    return {"content": body, "headers": {"content-type": "text/event-stream"}}


@pytest.fixture
def provider(google_script):
    cfg = ProviderConfig(api_key=KEY, base_url="http://gemini.test")
    return GoogleProvider(cfg, http_client=google_script.http())


def test_request_mapping(provider, google_script):
    google_script.reply(**sse_reply(chunk([{"text": "ok"}], finish="STOP")))
    params = {"max_tokens": 300, "temperature": 0.3, "top_p": 0.8, "stop": ["END"]}
    list(provider.stream(MESSAGES, "gemini-2.5-pro", params, TOOLS, SCHEMA))
    request = google_script.requests[-1]
    assert request.url.path.endswith("/models/gemini-2.5-pro:streamGenerateContent")
    assert request.url.params["alt"] == "sse" and request.headers["x-goog-api-key"] == KEY
    body = google_script.body
    assert body["systemInstruction"]["parts"] == [{"text": "Be brief."}]
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "weather in Paris?"}]},
        {"role": "model", "parts": [
            {"functionCall": {"name": "weather", "args": {"city": "Paris"}}}]},
        {"role": "user", "parts": [
            {"functionResponse": {"name": "weather", "response": {"result": "sunny"}}}]},
    ]  # fmt: skip
    declaration = body["tools"][0]["functionDeclarations"][0]
    assert (declaration["name"], declaration["description"]) == ("weather", "Get weather")
    assert CITY in declaration.values()
    config = body["generationConfig"]
    assert (config["maxOutputTokens"], config["temperature"], config["topP"]) == (300, 0.3, 0.8)
    assert config["stopSequences"] == ["END"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == SCHEMA


def test_streams_text_tool_call_and_usage(provider, google_script):
    usage = {"promptTokenCount": 12, "candidatesTokenCount": 6, "thoughtsTokenCount": 2,
             "cachedContentTokenCount": 4, "totalTokenCount": 20}  # fmt: skip
    google_script.reply(
        **sse_reply(
            chunk([{"text": "Hel"}]),
            chunk(
                [{"text": "lo"}, {"functionCall": {"name": "weather", "args": {"city": "Paris"}}}]
            ),
            chunk([], finish="STOP", usage=usage),
        )
    )
    out = list(provider.stream([{"role": "user", "content": "hi"}], "gemini-2.5-flash"))
    assert out == [
        TextDelta("Hel"),
        TextDelta("lo"),
        ToolCallDelta("call_1", "weather", {"city": "Paris"}),
        UsageDelta(8, 8, {"cache_read": 4, "reasoning": 2}),  # 12 prompt tokens, 4 of them cached
        DoneDelta("STOP"),
    ]


def test_errors_never_contain_the_key(provider, google_script):
    google_script.reply(
        status=400,
        json={
            "error": {
                "code": 400,
                "message": f"API key not valid: {KEY}",
                "status": "INVALID_ARGUMENT",
            }
        },
    )
    out = list(provider.stream([{"role": "user", "content": "hi"}], "gemini-2.5-pro"))
    assert len(out) == 1 and isinstance(out[0], ErrorDelta)
    assert "API key not valid" in out[0].message and "supersecret" not in out[0].message


def test_registered_by_name():
    assert providers.get_factory("google") is GoogleProvider
