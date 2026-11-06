import builtins
import datetime as dt
import importlib
import sys
from types import SimpleNamespace

from traceloom_sdk import observe
from traceloom_sdk.litellm import callback, failure_callback, success_callback

T0 = dt.datetime(2026, 11, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


def response(text="hi", usage=None):
    return SimpleNamespace(
        model="gpt-4o-2024",
        choices=[SimpleNamespace(message=SimpleNamespace(
            model_dump=lambda: {"role": "assistant", "content": text, "tool_calls": None}))],
        usage=usage or SimpleNamespace(prompt_tokens=10, completion_tokens=4, total_tokens=14,
                                       prompt_tokens_details=None),
    )  # fmt: skip


def call_kwargs(**over):
    base = {
        "model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}],
        "optional_params": {"temperature": 0.1, "stream_options": {"x": 1}},
        "litellm_params": {"metadata": {}}, "stream": False,
    }  # fmt: skip
    base.update(over)
    return base


def generation(recorder):
    create = recorder.of_type("generation-create")[0]["body"]
    update = next(
        e["body"] for e in recorder.of_type("generation-update") if e["body"]["id"] == create["id"]
    )
    return create, update


def test_success_records_generation_with_usage_cost_and_times(client, recorder):
    callback(call_kwargs(response_cost=0.0012), response(), T0, T0 + dt.timedelta(seconds=2))
    client.flush()
    create, update = generation(recorder)
    assert create["model"] == "gpt-4o" and create["input"] == [{"role": "user", "content": "hello"}]
    assert create["modelParameters"] == {"temperature": 0.1}
    assert create["startTime"].startswith("2026-11-01T12:00:00")
    assert update["endTime"].startswith("2026-11-01T12:00:02")
    assert update["output"] == {"role": "assistant", "content": "hi"}
    assert update["usageDetails"] == {"input": 10, "output": 4, "total": 14}
    assert update["costDetails"] == {"total": 0.0012} and update["model"] == "gpt-4o-2024"
    assert "level" not in update and "completionStartTime" not in update


def test_stream_records_time_to_first_token(client, recorder):
    first = T0 + dt.timedelta(milliseconds=300)
    callback(
        call_kwargs(stream=True, completion_start_time=first),
        response(),
        T0,
        T0 + dt.timedelta(seconds=1),
    )
    client.flush()
    update = generation(recorder)[1]
    assert update["completionStartTime"].startswith("2026-11-01T12:00:00.3")


def test_failure_gives_level_error(client, recorder):
    err = RuntimeError("rate limited")
    failure_callback(call_kwargs(exception=err), None, T0, T0 + dt.timedelta(seconds=1))
    client.flush()
    update = generation(recorder)[1]
    assert update["level"] == "ERROR" and update["statusMessage"] == "RuntimeError: rate limited"
    assert "output" not in update


def test_failure_with_exception_as_the_response_argument(client, recorder):
    callback(call_kwargs(), ValueError("boom"), T0, T0)
    client.flush()
    assert generation(recorder)[1]["level"] == "ERROR"


def test_nests_under_current_observation_and_metadata_overrides(client, recorder):
    @observe
    def work():
        success_callback(call_kwargs(), response(), T0, T0)

    work()
    success_callback(
        call_kwargs(litellm_params={"metadata": {"trace_id": "t-9", "parent_observation_id": "p-9",
                                                   "generation_name": "named"}}),
        response(), T0, T0,
    )  # fmt: skip
    client.flush()
    creates = recorder.of_type("generation-create")
    work_span = recorder.of_type("span-create")[0]["body"]
    assert creates[0]["body"]["parentObservationId"] == work_span["id"]
    assert (creates[1]["body"]["traceId"], creates[1]["body"]["parentObservationId"]) == (
        "t-9",
        "p-9",
    )
    assert creates[1]["body"]["name"] == "named"


def test_garbage_input_never_raises(client, sdk_logs):
    callback(None, None, None, None)
    callback({"messages": object()}, object(), "not-a-time", None)
    assert callback is success_callback is failure_callback


def test_imports_without_litellm(monkeypatch):
    real_import = builtins.__import__

    def deny(name, *args, **kwargs):
        if name.startswith("litellm"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny)
    monkeypatch.delitem(sys.modules, "traceloom_sdk.litellm", raising=False)
    assert importlib.import_module("traceloom_sdk.litellm").callback
