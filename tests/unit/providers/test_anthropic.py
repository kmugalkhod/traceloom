import pytest

from tests.unit.providers.conftest import sse
from traceloom.llm.providers import (
    DoneDelta,
    ErrorDelta,
    ProviderConfig,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
)
from traceloom.llm.providers.anthropic import AnthropicProvider, is_five_family

KEY = "sk-ant-api03-supersecretvalue"
MSG_USAGE = {"input_tokens": 12, "output_tokens": 1, "cache_read_input_tokens": 2,
             "cache_creation_input_tokens": 0}  # fmt: skip


def message(stop=None):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [], "stop_reason": stop, "stop_sequence": None, "usage": MSG_USAGE,
    }  # fmt: skip


def events(*blocks, stop="end_turn", out=6):
    """blocks: ("text", [chunks]) or ("tool", id, name, [json chunks])."""
    evs = [("message_start", {"type": "message_start", "message": message()})]
    for i, block in enumerate(blocks):
        if block[0] == "text":
            start = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": t} for t in block[1]]
        else:
            start = {"type": "tool_use", "id": block[1], "name": block[2], "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": j} for j in block[3]]
        evs.append(("content_block_start", {"type": "content_block_start", "index": i,
                                            "content_block": start}))  # fmt: skip
        evs += [("content_block_delta", {"type": "content_block_delta", "index": i, "delta": d})
                for d in deltas]  # fmt: skip
        evs.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    evs += [("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": stop, "stop_sequence": None},
                               "usage": {"output_tokens": out}}),
            ("message_stop", {"type": "message_stop"})]  # fmt: skip
    return sse(evs)


@pytest.fixture
def provider(anthropic_script):
    cfg = ProviderConfig(api_key=KEY, base_url="http://claude.test")
    return AnthropicProvider(cfg, http_client=anthropic_script.http())


MESSAGES = [
    {"role": "system", "content": "Be brief."},
    {"role": "user", "content": "weather in Paris?"},
    {"role": "assistant", "content": "Checking.",
     "tool_calls": [{"id": "t1", "name": "weather", "arguments": {"city": "Paris"}},
                    {"id": "t2", "name": "time", "arguments": {}}]},
    {"role": "tool", "tool_call_id": "t1", "content": "sunny"},
    {"role": "tool", "tool_call_id": "t2", "content": "noon"},
]  # fmt: skip
CITY = {"type": "object", "properties": {"city": {"type": "string"}}}
TOOLS = [{"name": "weather", "description": "Get weather", "parameters": CITY}]


def test_request_carries_exact_messages_system_params_tools(provider, anthropic_script):
    anthropic_script.reply(**events(("text", ["ok"])))
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    out = list(
        provider.stream(
            MESSAGES, "claude-haiku-4-5-20251001",
            {"max_tokens": 500, "temperature": 0.2, "top_p": 0.9, "stop": ["END"]},
            TOOLS, schema,
        )
    )  # fmt: skip
    assert out[-1] == DoneDelta("end_turn")
    request = anthropic_script.requests[-1]
    assert (
        request.url.path == "/v1/messages" and request.headers["authorization"] == f"Bearer {KEY}"
    )
    body = anthropic_script.body
    assert body["model"] == "claude-haiku-4-5-20251001" and body["stream"] is True
    assert body["system"] == "Be brief."
    assert (body["max_tokens"], body["temperature"], body["top_p"]) == (500, 0.2, 0.9)
    assert body["stop_sequences"] == ["END"]
    assert body["messages"] == [
        {"role": "user", "content": "weather in Paris?"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "t1", "name": "weather", "input": {"city": "Paris"}},
            {"type": "tool_use", "id": "t2", "name": "time", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "sunny"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "noon"}]},
    ]  # fmt: skip
    assert body["tools"] == [{"name": "weather", "description": "Get weather",
                              "input_schema": TOOLS[0]["parameters"]}]  # fmt: skip
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": schema}}
    assert "thinking" not in body and body["messages"][-1]["role"] == "user"  # no prefill


def test_five_family_has_no_budget_tokens_or_sampling(provider, anthropic_script):
    assert is_five_family("claude-sonnet-5-5") and is_five_family("claude-fable-5-1")
    assert not is_five_family("claude-haiku-4-5-20251001")
    anthropic_script.reply(**events(("text", ["x"])))
    list(provider.stream([{"role": "user", "content": "hi"}], "claude-opus-5-5",
                         {"temperature": 0.5, "thinking": True, "thinking_budget": 4000,
                          "effort": "high"}))  # fmt: skip
    body = anthropic_script.body
    assert body["thinking"] == {"type": "adaptive"}
    assert "temperature" not in body and "budget_tokens" not in str(body)
    assert body["output_config"] == {"effort": "high"}


def test_older_model_takes_a_thinking_budget(provider, anthropic_script):
    anthropic_script.reply(**events(("text", ["x"])))
    list(provider.stream([{"role": "user", "content": "hi"}], "claude-haiku-4-5-20251001",
                         {"thinking_budget": 2048, "max_tokens": 4096}))  # fmt: skip
    assert anthropic_script.body["thinking"] == {"type": "enabled", "budget_tokens": 2048}


def test_text_tool_call_and_usage_stream_in_order(provider, anthropic_script):
    call = ("tool", "toolu_1", "weather", ['{"city":', ' "Paris"}'])
    anthropic_script.reply(**events(("text", ["Hel", "lo"]), call))
    out = list(provider.stream([{"role": "user", "content": "hi"}], "claude-sonnet-5-5"))
    assert out == [
        TextDelta("Hel"), TextDelta("lo"),
        ToolCallDelta("toolu_1", "weather", {"city": "Paris"}),
        UsageDelta(12, 6, {"cache_read": 2}),
        DoneDelta("end_turn"),
    ]  # fmt: skip


def test_refusal_is_an_inline_message(provider, anthropic_script):
    anthropic_script.reply(**events(stop="refusal", out=0))
    out = list(provider.stream([{"role": "user", "content": "hi"}], "claude-opus-5-5"))
    assert isinstance(out[-2], ErrorDelta) and "declined" in out[-2].message
    assert out[-1] == DoneDelta("refusal")


def test_errors_contain_no_key(provider, anthropic_script):
    anthropic_script.reply(
        status=401,
        json={"type": "error",
              "error": {"type": "authentication_error", "message": f"invalid x-api-key {KEY}"}},
    )  # fmt: skip
    out = list(provider.stream([{"role": "user", "content": "hi"}], "claude-sonnet-5-5"))
    assert len(out) == 1 and isinstance(out[0], ErrorDelta)
    assert "invalid x-api-key" in out[0].message and KEY not in out[0].message
    assert "supersecret" not in out[0].message


def test_registered_by_name():
    from traceloom.llm import providers

    assert providers.get_factory("anthropic") is AnthropicProvider
