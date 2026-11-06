import json

import pytest

openai = pytest.importorskip("openai")

from traceloom_sdk import observe  # noqa: E402
from traceloom_sdk.openai import wrap  # noqa: E402

BASE = "http://llm.test/v1"
CHAT_URL = f"{BASE}/chat/completions"
RESPONSES_URL = f"{BASE}/responses"
MESSAGES = [{"role": "user", "content": "say hi"}]


def chat_json(content="Hello!", **extra):
    message = {"role": "assistant", "content": content}
    message.update(extra)
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 4},
        },
    }


def sse(*events):  # a reply spec for Script.reply(**spec)
    out = ""
    for event in events:
        if isinstance(event, tuple):
            out += f"event: {event[0]}\ndata: {json.dumps(event[1])}\n\n"
        else:
            out += f"data: {event if isinstance(event, str) else json.dumps(event)}\n\n"
    return {"content": out, "headers": {"content-type": "text/event-stream"}}


def chunk(delta=None, usage=None, finish=None):
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-4o-mini",
        "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}],
        "usage": usage,
    }


def generation(recorder):
    create = recorder.of_type("generation-create")[0]["body"]
    update = next(
        e["body"] for e in recorder.of_type("generation-update") if e["body"]["id"] == create["id"]
    )
    return create, update


@pytest.fixture
def script(openai_script):
    return openai_script


@pytest.fixture
def sync_client(client, script):
    return wrap(
        openai.OpenAI(api_key="sk-x", base_url=BASE, max_retries=0, http_client=script.http())
    )


@pytest.fixture
def async_client(client, script):
    http = script.http(is_async=True)
    return wrap(openai.AsyncOpenAI(api_key="sk-x", base_url=BASE, max_retries=0, http_client=http))


def test_chat_completion_records_a_generation(script, sync_client, client, recorder):
    script.reply(json=chat_json())
    with_obs = observe(sync_client.chat.completions.create)
    response = with_obs(model="gpt-4o-mini", messages=MESSAGES, temperature=0.2, max_tokens=50)
    assert response.choices[0].message.content == "Hello!"  # the provider's own return value
    client.flush()
    create, update = generation(recorder)
    assert create["model"] == "gpt-4o-mini" and create["input"] == MESSAGES
    assert create["modelParameters"] == {"temperature": 0.2, "max_tokens": 50}
    assert update["output"] == {"role": "assistant", "content": "Hello!"}
    assert update["usageDetails"] == {"input": 6, "output": 5, "total": 15, "cache_read": 4}
    assert update["model"] == "gpt-4o-mini-2024-07-18"
    assert create["parentObservationId"]  # nested under the observed function


def test_tool_calls_are_captured_in_the_output(script, sync_client, client, recorder):
    tool = [{"id": "t1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    script.reply(json=chat_json(None, tool_calls=tool))
    sync_client.chat.completions.create(model="m", messages=MESSAGES)
    client.flush()
    assert generation(recorder)[1]["output"]["tool_calls"][0]["function"]["name"] == "f"


def test_stream_records_ttft_and_assembles_output(script, sync_client, client, recorder):
    script.reply(
        **sse(
            chunk({"role": "assistant", "content": ""}),
            chunk({"content": "Hel"}),
            chunk({"content": "lo"}, finish=None),
            chunk({}, finish="stop"),
            chunk(None, usage={"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}),
            "[DONE]",
        )
    )
    stream = sync_client.chat.completions.create(model="m", messages=MESSAGES, stream=True)
    pieces = [c.choices[0].delta.content for c in stream if c.choices]
    assert "".join(p for p in pieces if p) == "Hello"
    client.flush()
    create, update = generation(recorder)
    assert update["output"] == {"role": "assistant", "content": "Hello"}
    assert update["usageDetails"] == {"input": 7, "output": 2, "total": 9}
    assert update["completionStartTime"] >= create["startTime"]
    assert "level" not in update


def test_stream_tool_call_deltas_are_assembled(script, sync_client, client, recorder):
    call = lambda args, name="", id=None: {  # noqa: E731
        "tool_calls": [{"index": 0, "id": id, "function": {"name": name, "arguments": args}}]
    }
    script.reply(
        **sse(chunk(call("", "search", "t1")), chunk(call('{"q":')), chunk(call('"x"}')), "[DONE]")
    )
    list(sync_client.chat.completions.create(model="m", messages=MESSAGES, stream=True))
    client.flush()
    out = generation(recorder)[1]["output"]
    assert out["tool_calls"] == [
        {"id": "t1", "type": "function", "function": {"name": "search", "arguments": '{"q":"x"}'}}
    ]


async def test_async_chat_and_stream(script, async_client, client, recorder):
    script.reply(json=chat_json("async hi"))
    script.reply(**sse(chunk({"content": "a"}), chunk({"content": "b"}), "[DONE]"))
    response = await async_client.chat.completions.create(model="m", messages=MESSAGES)
    assert response.choices[0].message.content == "async hi"
    stream = await async_client.chat.completions.create(model="m", messages=MESSAGES, stream=True)
    async for _ in stream:
        pass
    client.flush()
    updates = [e["body"] for e in recorder.of_type("generation-update")]
    assert updates[0]["output"]["content"] == "async hi"
    assert updates[1]["output"]["content"] == "ab" and "completionStartTime" in updates[1]


def responses_json(text="Hi there"):
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "model": "gpt-4o",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "m1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 8,
            "output_tokens": 3,
            "total_tokens": 11,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


def test_responses_api_and_stream(script, sync_client, client, recorder):
    delta = lambda text, n: ("response.output_text.delta", {  # noqa: E731
        "type": "response.output_text.delta", "item_id": "m1", "output_index": 0,
        "content_index": 0, "delta": text, "sequence_number": n,
    })  # fmt: skip
    done = ("response.completed", {"type": "response.completed", "sequence_number": 9,
                                    "response": responses_json("Hi there")})  # fmt: skip
    script.reply(json=responses_json())
    script.reply(**sse(delta("Hi ", 1), delta("there", 2), done))
    response = sync_client.responses.create(
        model="gpt-4o", input="greet me", instructions="be nice"
    )
    assert response.output_text == "Hi there"
    for _ in sync_client.responses.create(model="gpt-4o", input="greet me", stream=True):
        pass
    client.flush()
    creates = [e["body"] for e in recorder.of_type("generation-create")]
    updates = [e["body"] for e in recorder.of_type("generation-update")]
    assert creates[0]["input"] == "greet me" and creates[0]["modelParameters"] == {
        "instructions": "be nice"
    }
    for update in updates:
        assert update["output"] == "Hi there"
        assert update["usageDetails"] == {"input": 8, "output": 3, "total": 11}
    assert "completionStartTime" in updates[1]


def test_provider_error_sets_error_level_and_reraises_the_original(
    script, sync_client, client, recorder
):
    script.reply(400, json={"error": {"message": "bad model", "type": "invalid"}})
    with pytest.raises(openai.BadRequestError) as caught:
        sync_client.chat.completions.create(model="nope", messages=MESSAGES)
    assert "bad model" in str(caught.value)
    client.flush()
    update = generation(recorder)[1]
    assert update["level"] == "ERROR" and "BadRequestError" in update["statusMessage"]


def test_mid_stream_error_marks_the_generation(script, sync_client, client, recorder):
    def boom():
        yield b'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"m",'
        yield b'"choices":[{"index":0,"delta":{"content":"par"},"finish_reason":null}]}\n\n'
        raise script.module.ReadError("connection dropped")

    script.reply(content=boom(), headers={"content-type": "text/event-stream"})
    with pytest.raises(openai.APIError):
        list(sync_client.chat.completions.create(model="m", messages=MESSAGES, stream=True))
    client.flush()
    assert generation(recorder)[1]["level"] == "ERROR"


def test_instrumentation_failures_never_reach_the_caller(
    script, sync_client, client, monkeypatch, sdk_logs
):
    script.reply(json=chat_json())
    monkeypatch.setattr(
        client, "start_generation", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sdk"))
    )
    response = sync_client.chat.completions.create(model="m", messages=MESSAGES)
    assert response.choices[0].message.content == "Hello!"
    assert "could not start generation" in sdk_logs.text


def test_unreadable_response_shape_is_ignored(client, recorder):
    class Odd:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    return "not a response object"

    odd = wrap(Odd)
    assert odd.chat.completions.create(model="m", messages=MESSAGES) == "not a response object"
    client.flush()
    assert generation(recorder)[1].get("output") is None


def test_wrapping_twice_traces_once_and_returns_the_client(client, recorder, script):
    raw = openai.OpenAI(api_key="k", base_url=BASE, max_retries=0, http_client=script.http())
    assert wrap(wrap(raw)) is raw
    script.reply(json=chat_json())
    raw.chat.completions.create(model="m", messages=MESSAGES)
    client.flush()
    assert len(recorder.of_type("generation-create")) == 1
