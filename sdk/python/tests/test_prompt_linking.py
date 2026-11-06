import json

import pytest

from traceloom_sdk import (
    ChatPrompt,
    TextPrompt,
    observe,
    start_generation,
    update_current_observation,
)
from traceloom_sdk.prompts import fallback_prompt

LINKED = TextPrompt("support", "Hi {{name}}", version=2)
FALLBACK = fallback_prompt("support", "Hi {{name}}")


def generation(recorder):
    create = recorder.of_type("generation-create")[0]["body"]
    updates = [
        e["body"] for e in recorder.of_type("generation-update") if e["body"]["id"] == create["id"]
    ]
    return create, (updates[0] if updates else {})


def linked(*bodies):
    merged = {}
    for body in bodies:
        merged.update(body)
    return merged.get("promptName"), merged.get("promptVersion")


def test_start_generation_prompt_sets_name_and_version(client, recorder):
    with start_generation("g", prompt=LINKED):
        pass
    client.flush()
    assert linked(*generation(recorder)) == ("support", 2)
    assert generation(recorder)[0]["promptName"] == "support"  # in the create event itself


def test_chat_prompt_links_too(client, recorder):
    chat = ChatPrompt("chat-prompt", [{"role": "user", "content": "x"}], version=5)
    with start_generation("g", prompt=chat):
        pass
    client.flush()
    assert linked(*generation(recorder)) == ("chat-prompt", 5)


def test_fallback_prompt_is_not_linked(client, recorder):
    assert FALLBACK.is_fallback
    with start_generation("g", prompt=FALLBACK) as gen:
        gen.update(prompt=FALLBACK)
    client.flush()
    assert linked(*generation(recorder)) == (None, None)


def test_update_current_observation_links(client, recorder):
    with start_generation("g"):
        update_current_observation(prompt=LINKED)
    client.flush()
    assert linked(*generation(recorder)) == ("support", 2)


def test_observe_generation_links_via_update_current_observation(client, recorder):
    @observe(as_type="generation", name="answer")
    def answer():
        update_current_observation(prompt=LINKED, model="m")
        return "ok"

    assert answer() == "ok"
    client.flush()
    assert linked(*generation(recorder)) == ("support", 2)


@pytest.mark.parametrize("bad", [None, "support", 3, object(), TextPrompt("x", "y"), {"name": "x"}])
def test_unusable_prompts_link_nothing_and_never_raise(client, recorder, bad):
    with start_generation("g", prompt=bad) as gen:
        gen.update(prompt=bad)
        update_current_observation(prompt=bad)
    client.flush()
    assert linked(*generation(recorder)) == (None, None)
    assert recorder.of_type("generation-create")


def test_broken_prompt_object_never_raises(client, recorder):
    class Broken:
        is_fallback = False

        @property
        def name(self):
            raise RuntimeError("boom")

    with start_generation("g", prompt=Broken()):
        pass
    client.flush()
    assert linked(*generation(recorder)) == (None, None)


# --- provider wrappers ---------------------------------------------------------------------

MESSAGES = [{"role": "user", "content": "say hi"}]


def test_openai_strips_traceloom_prompt_and_links(openai_script, client, recorder):
    openai = pytest.importorskip("openai")
    from traceloom_sdk.openai import wrap

    from .test_openai import BASE, chat_json

    openai_script.reply(json=chat_json())
    sdk = wrap(
        openai.OpenAI(api_key="k", base_url=BASE, max_retries=0, http_client=openai_script.http())
    )
    sdk.chat.completions.create(model="m", messages=MESSAGES, traceloom_prompt=LINKED)
    client.flush()
    sent = json.loads(openai_script.requests[0].content)
    assert "traceloom_prompt" not in sent and sent["messages"] == MESSAGES
    create, update = generation(recorder)
    assert linked(create, update) == ("support", 2)
    assert "traceloom_prompt" not in create["modelParameters"]


def test_openai_fallback_not_linked_but_still_stripped(openai_script, client, recorder):
    openai = pytest.importorskip("openai")
    from traceloom_sdk.openai import wrap

    from .test_openai import BASE, chat_json

    openai_script.reply(json=chat_json())
    sdk = wrap(
        openai.OpenAI(api_key="k", base_url=BASE, max_retries=0, http_client=openai_script.http())
    )
    sdk.chat.completions.create(model="m", messages=MESSAGES, traceloom_prompt=FALLBACK)
    client.flush()
    assert "traceloom_prompt" not in json.loads(openai_script.requests[0].content)
    assert linked(*generation(recorder)) == (None, None)


def test_openai_responses_api_strips_it_too(openai_script, client, recorder):
    openai = pytest.importorskip("openai")
    from traceloom_sdk.openai import wrap

    from .test_openai import BASE

    body = {
        "id": "r1", "object": "response", "created_at": 1, "model": "m", "status": "completed",
        "output": [], "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 0}},
    }  # fmt: skip
    openai_script.reply(json=body)
    sdk = wrap(
        openai.OpenAI(api_key="k", base_url=BASE, max_retries=0, http_client=openai_script.http())
    )
    sdk.responses.create(model="m", input="hi", traceloom_prompt=LINKED)
    client.flush()
    assert "traceloom_prompt" not in json.loads(openai_script.requests[0].content)
    assert linked(*generation(recorder)) == ("support", 2)


def test_anthropic_strips_traceloom_prompt_and_links(anthropic_script, client, recorder):
    anthropic = pytest.importorskip("anthropic")
    from traceloom_sdk.anthropic import wrap

    from .test_anthropic import BASE, message_json

    anthropic_script.reply(json=message_json())
    sdk = wrap(
        anthropic.Anthropic(
            api_key="k", base_url=BASE, max_retries=0, http_client=anthropic_script.http()
        )
    )
    sdk.messages.create(model="m", max_tokens=5, messages=MESSAGES, traceloom_prompt=LINKED)
    client.flush()
    sent = json.loads(anthropic_script.requests[0].content)
    assert "traceloom_prompt" not in sent
    assert linked(*generation(recorder)) == ("support", 2)


def test_anthropic_stream_helper_strips_and_links(anthropic_script, client, recorder):
    anthropic = pytest.importorskip("anthropic")
    from traceloom_sdk.anthropic import wrap

    from .test_anthropic import BASE, stream_response

    anthropic_script.reply(**stream_response("Hel", "lo"))
    sdk = wrap(
        anthropic.Anthropic(
            api_key="k", base_url=BASE, max_retries=0, http_client=anthropic_script.http()
        )
    )
    with sdk.messages.stream(
        model="m", max_tokens=5, messages=MESSAGES, traceloom_prompt=LINKED
    ) as stream:
        assert "".join(stream.text_stream) == "Hello"
    client.flush()
    assert "traceloom_prompt" not in json.loads(anthropic_script.requests[0].content)
    assert linked(*generation(recorder)) == ("support", 2)


def test_anthropic_fallback_not_linked(anthropic_script, client, recorder):
    anthropic = pytest.importorskip("anthropic")
    from traceloom_sdk.anthropic import wrap

    from .test_anthropic import BASE, message_json

    anthropic_script.reply(json=message_json())
    sdk = wrap(
        anthropic.Anthropic(
            api_key="k", base_url=BASE, max_retries=0, http_client=anthropic_script.http()
        )
    )
    sdk.messages.create(model="m", max_tokens=5, messages=MESSAGES, traceloom_prompt=FALLBACK)
    client.flush()
    assert "traceloom_prompt" not in json.loads(anthropic_script.requests[0].content)
    assert linked(*generation(recorder)) == (None, None)


def test_streaming_openai_create_with_prompt(openai_script, client, recorder):
    openai = pytest.importorskip("openai")
    from traceloom_sdk.openai import wrap

    from .test_openai import BASE, chunk, sse

    openai_script.reply(
        **sse(chunk({"role": "assistant", "content": "Hi"}), chunk({}, finish="stop"), "[DONE]")
    )
    sdk = wrap(
        openai.OpenAI(api_key="k", base_url=BASE, max_retries=0, http_client=openai_script.http())
    )
    stream = sdk.chat.completions.create(
        model="m", messages=MESSAGES, stream=True, traceloom_prompt=LINKED
    )
    assert list(stream)
    client.flush()
    assert "traceloom_prompt" not in json.loads(openai_script.requests[0].content)
    assert linked(*generation(recorder)) == ("support", 2)
