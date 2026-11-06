"""Traceloom Python SDK."""

from __future__ import annotations

from typing import Any

from traceloom_sdk.client import Traceloom, get_client, set_default_client
from traceloom_sdk.objects import Event, Generation, Observation, Span
from traceloom_sdk.observe import observe
from traceloom_sdk.prompts import ChatPrompt, PromptNotFound, TextPrompt

__version__ = "0.1.0"


def start_span(name: str | None = None, **fields: Any) -> Span:
    return get_client().start_span(name, **fields)


def start_generation(name: str | None = None, **fields: Any) -> Generation:
    return get_client().start_generation(name, **fields)


def start_observation(name: str | None = None, as_type: str = "span", **fields: Any):
    return get_client().start_observation(name, as_type, **fields)


def event(name: str | None = None, **fields: Any) -> Event:
    return get_client().event(name, **fields)


def update_current_trace(**fields: Any) -> None:
    get_client().update_current_trace(**fields)


def update_current_observation(**fields: Any) -> None:
    get_client().update_current_observation(**fields)


def score(
    name: str,
    value: Any,
    trace_id: str | None = None,
    observation_id: str | None = None,
    data_type: str | None = None,
    comment: str | None = None,
) -> None:
    get_client().score(name, value, trace_id, observation_id, data_type, comment)


def get_prompt(name: str, **options: Any) -> TextPrompt | ChatPrompt:
    return get_client().get_prompt(name, **options)


async def aget_prompt(name: str, **options: Any) -> TextPrompt | ChatPrompt:
    return await get_client().aget_prompt(name, **options)


def get_current_trace_id() -> str | None:
    return get_client().get_current_trace_id()


def flush(timeout: float | None = None) -> bool:
    return get_client().flush(timeout)


__all__ = [
    "ChatPrompt",
    "PromptNotFound",
    "TextPrompt",
    "aget_prompt",
    "Event",
    "Generation",
    "Observation",
    "Span",
    "Traceloom",
    "event",
    "flush",
    "get_client",
    "get_current_trace_id",
    "get_prompt",
    "observe",
    "score",
    "set_default_client",
    "start_generation",
    "start_observation",
    "start_span",
    "update_current_observation",
    "update_current_trace",
]
