import json

import pytest

anthropic = pytest.importorskip("anthropic")

from traceloom_sdk.anthropic import wrap  # noqa: E402

BASE = "http://claude.test"
URL = f"{BASE}/v1/messages"
MESSAGES = [{"role": "user", "content": "say hi"}]
USAGE = {"input_tokens": 12, "output_tokens": 6, "cache_read_input_tokens": 2,
         "cache_creation_input_tokens": 0}  # fmt: skip


def message_json(text="Hello!", usage=None):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        "stop_sequence": None, "usage": usage or USAGE,
    }  # fmt: skip


def stream_response(*texts):
    events = [
        ("message_start", {"type": "message_start", "message": {**message_json(""), "content": [],
                                                                "usage": {"input_tokens": 12, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
    ]  # fmt: skip
    for text in texts:
        events.append(("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": text}}))  # fmt: skip
    events += [
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 6}}),
        ("message_stop", {"type": "message_stop"}),
    ]  # fmt: skip
    body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
    return {"content": body, "headers": {"content-type": "text/event-stream"}}


def generation(recorder):
    create = recorder.of_type("generation-create")[0]["body"]
    update = next(
        e["body"] for e in recorder.of_type("generation-update") if e["body"]["id"] == create["id"]
    )
    return create, update


@pytest.fixture
def script(anthropic_script):
    return anthropic_script


@pytest.fixture
def sync_client(client, script):
    http = script.http()
    return wrap(anthropic.Anthropic(api_key="k", base_url=BASE, max_retries=0, http_client=http))


@pytest.fixture
def async_client(client, script):
    http = script.http(is_async=True)
    return wrap(
        anthropic.AsyncAnthropic(api_key="k", base_url=BASE, max_retries=0, http_client=http)
    )


def test_messages_create_records_a_generation(script, sync_client, client, recorder):
    script.reply(json=message_json())
    response = sync_client.messages.create(
        model="claude-sonnet-5-5",
        max_tokens=100,
        stop_sequences=["END"],
        system="be brief",
        messages=MESSAGES,
    )
    assert response.content[0].text == "Hello!"
    client.flush()
    create, update = generation(recorder)
    assert create["model"] == "claude-sonnet-5-5" and create["input"] == MESSAGES
    assert create["modelParameters"] == {
        "max_tokens": 100,
        "stop_sequences": ["END"],
        "system": "be brief",
    }
    assert update["output"] == "Hello!"
    assert update["usageDetails"] == {"input": 12, "output": 6, "cache_read": 2, "total": 20}


def test_tool_use_output_is_kept_as_blocks(script, sync_client, client, recorder):
    body = message_json()
    body["content"] = [{"type": "tool_use", "id": "tu1", "name": "search", "input": {"q": "x"}}]
    script.reply(json=body)
    sync_client.messages.create(model="m", max_tokens=5, messages=MESSAGES)
    client.flush()
    assert generation(recorder)[1]["output"][0]["name"] == "search"


def test_create_with_stream_records_ttft_and_output(script, sync_client, client, recorder):
    script.reply(**stream_response("Hel", "lo"))
    stream = sync_client.messages.create(model="m", max_tokens=5, messages=MESSAGES, stream=True)
    events = [e.type for e in stream]
    assert events[0] == "message_start" and events[-1] == "message_stop"
    client.flush()
    create, update = generation(recorder)
    assert update["output"] == "Hello"
    assert update["usageDetails"] == {"input": 12, "output": 6, "total": 18}
    assert update["completionStartTime"] >= create["startTime"]


def test_messages_stream_manager_text_stream(script, sync_client, client, recorder):
    script.reply(**stream_response("Hi ", "you"))
    with sync_client.messages.stream(model="m", max_tokens=5, messages=MESSAGES) as stream:
        text = "".join(stream.text_stream)
    assert text == "Hi you"
    client.flush()
    create, update = generation(recorder)
    assert update["output"] == "Hi you" and update["usageDetails"]["output"] == 6
    assert "completionStartTime" in update


def test_messages_stream_manager_event_iteration(script, sync_client, client, recorder):
    script.reply(**stream_response("A", "B"))
    with sync_client.messages.stream(model="m", max_tokens=5, messages=MESSAGES) as stream:
        for _ in stream:
            pass
        final = stream.get_final_message()
    assert final.content[0].text == "AB"
    client.flush()
    assert generation(recorder)[1]["output"] == "AB"


async def test_async_create_and_stream_manager(script, async_client, client, recorder):
    script.reply(json=message_json("async"))
    script.reply(**stream_response("x", "y"))
    response = await async_client.messages.create(model="m", max_tokens=5, messages=MESSAGES)
    assert response.content[0].text == "async"
    async with async_client.messages.stream(model="m", max_tokens=5, messages=MESSAGES) as stream:
        text = "".join([t async for t in stream.text_stream])
    assert text == "xy"
    client.flush()
    updates = [e["body"] for e in recorder.of_type("generation-update")]
    assert [u["output"] for u in updates] == ["async", "xy"]
    assert "completionStartTime" in updates[1]


def test_provider_error_sets_error_level_and_reraises_the_original(
    script, sync_client, client, recorder
):
    script.reply(
        401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}
    )
    with pytest.raises(anthropic.AuthenticationError):
        sync_client.messages.create(model="m", max_tokens=5, messages=MESSAGES)
    client.flush()
    update = generation(recorder)[1]
    assert update["level"] == "ERROR" and "AuthenticationError" in update["statusMessage"]


def test_stream_manager_errors_are_recorded(script, sync_client, client, recorder):
    script.reply(
        529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    )
    with pytest.raises(anthropic.APIStatusError):
        with sync_client.messages.stream(model="m", max_tokens=5, messages=MESSAGES) as stream:
            list(stream)
    client.flush()
    assert generation(recorder)[1]["level"] == "ERROR"


def test_instrumentation_failures_never_reach_the_caller(
    script, sync_client, client, monkeypatch, sdk_logs
):
    script.reply(json=message_json())
    monkeypatch.setattr(
        client, "start_generation", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sdk"))
    )
    assert (
        sync_client.messages.create(model="m", max_tokens=5, messages=MESSAGES).content[0].text
        == "Hello!"
    )
