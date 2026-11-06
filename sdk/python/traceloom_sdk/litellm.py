"""LiteLLM callback, usable without importing litellm:

    import litellm
    from traceloom_sdk.litellm import callback
    litellm.success_callback = [callback]
    litellm.failure_callback = [callback]

Records every call as a generation: model, parameters, messages, output, usage, cost when LiteLLM
computed it, time to first token for streams, and level ERROR with the message on failure.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Dict

from traceloom_sdk import context, objects
from traceloom_sdk._llm import get, plain, usage_dict
from traceloom_sdk.client import get_client

log = logging.getLogger("traceloom")


def _usage(response: Any) -> Dict[str, int]:
    usage = get(response, "usage")
    if usage is None:
        return {}
    prompt = get(usage, "prompt_tokens", default=0)
    cached = get(usage, "prompt_tokens_details", "cached_tokens", default=0)
    return usage_dict(
        input=max(prompt - cached, 0),
        output=get(usage, "completion_tokens", default=0),
        total=get(usage, "total_tokens", default=0),
        cache_read=cached,
    )


def _output(response: Any) -> Any:
    choices = get(response, "choices", default=[]) or []
    messages = [plain(get(c, "message")) for c in choices]
    return messages[0] if len(messages) == 1 else (messages or None)


def _aware(value: Any) -> Any:
    if isinstance(value, _dt.datetime) and value.tzinfo is None:
        return value.replace(tzinfo=_dt.timezone.utc)
    return value


def callback(
    kwargs: Dict[str, Any], completion_response: Any, start_time: Any, end_time: Any
) -> None:
    """LiteLLM `(kwargs, response, start_time, end_time)` callback. Never raises."""
    try:
        _record(kwargs or {}, completion_response, start_time, end_time)
    except Exception:
        log.exception("Traceloom LiteLLM callback failed")


success_callback = callback
failure_callback = callback


def _record(kwargs: Dict[str, Any], response: Any, start_time: Any, end_time: Any) -> None:
    error = kwargs.get("exception") or (response if isinstance(response, BaseException) else None)
    params = dict(kwargs.get("optional_params") or {})
    params.pop("stream_options", None)
    meta = get(kwargs, "litellm_params", "metadata", default={}) or {}
    fields: Dict[str, Any] = {
        "input": plain(kwargs.get("messages") or kwargs.get("input")),
        "model": kwargs.get("model"),
        "model_parameters": plain(params) or None,
        "start_time": _aware(start_time),
    }
    client = get_client()
    ambient = context.current_observation()
    trace_id = meta.get("trace_id") or (ambient.trace_id if ambient else None)
    parent_id = meta.get("parent_observation_id") or (ambient.id if ambient else None)
    gen = objects.start(
        client, "generation", meta.get("generation_name") or "litellm", fields,
        trace_id=trace_id, parent_id=parent_id if trace_id else None,
    )  # fmt: skip
    end: Dict[str, Any] = {"end_time": _aware(end_time)}
    first = kwargs.get("completion_start_time")
    if first is not None and kwargs.get("stream"):
        end["completion_start_time"] = _aware(first)
    if error is not None:
        objects.mark_error(
            gen, error if isinstance(error, BaseException) else RuntimeError(str(error))
        )
    else:
        end["output"] = _output(response)
        end["usage_details"] = _usage(response)
        returned = get(response, "model")
        if returned:
            end["model"] = returned
        cost = kwargs.get("response_cost")
        if isinstance(cost, (int, float)) and cost > 0:
            end["cost_details"] = {"total": float(cost)}
    gen.end(**end)


__all__ = ["callback", "failure_callback", "success_callback"]
