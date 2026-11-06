"""Shared fixtures data for the OpenAI-family adapter tests."""

from __future__ import annotations

import json

MESSAGES = [
    {"role": "system", "content": "Be brief."},
    {"role": "user", "content": "weather in Paris?"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "name": "weather", "arguments": {"city": "Paris"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
]  # fmt: skip
CITY = {"type": "object", "properties": {"city": {"type": "string"}}}
TOOLS = [{"name": "weather", "description": "Get weather", "parameters": CITY}]
SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}
PARAMS = {"max_tokens": 300, "temperature": 0.3, "top_p": 0.8, "stop": ["END"]}

EXPECTED_MESSAGES = [
    {"role": "system", "content": "Be brief."},
    {"role": "user", "content": "weather in Paris?"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "weather", "arguments": '{"city": "Paris"}'}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
]  # fmt: skip
EXPECTED_TOOLS = [{"type": "function", "function": {
    "name": "weather", "description": "Get weather", "parameters": CITY}}]  # fmt: skip


def chunk(delta=None, finish=None, usage=None, model="gpt-4o"):
    return {
        "id": "c1", "object": "chat.completion.chunk", "created": 1, "model": model,
        "choices": [] if delta is None and finish is None else
        [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
        **({"usage": usage} if usage else {}),
    }  # fmt: skip


USAGE = {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18,
         "prompt_tokens_details": {"cached_tokens": 4},
         "completion_tokens_details": {"reasoning_tokens": 2}}  # fmt: skip


def stream_reply():
    chunks = [
        chunk({"role": "assistant", "content": "Hel"}),
        chunk({"content": "lo"}),
        chunk({"tool_calls": [{"index": 0, "id": "call_9", "type": "function",
                               "function": {"name": "weather", "arguments": '{"city":'}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": ' "Paris"}'}}]}),
        chunk({}, finish="tool_calls"),
        chunk(usage=USAGE),
    ]  # fmt: skip
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return {"content": body, "headers": {"content-type": "text/event-stream"}}
