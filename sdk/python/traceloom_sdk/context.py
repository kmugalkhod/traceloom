"""Current-observation stack in a contextvar, so it follows threads and asyncio tasks."""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from traceloom_sdk.objects import Observation

_stack: contextvars.ContextVar[tuple[Observation, ...]] = contextvars.ContextVar(
    "traceloom_stack", default=()
)


def push(observation: Observation) -> contextvars.Token:
    return _stack.set(_stack.get() + (observation,))


def pop(token: contextvars.Token) -> None:
    try:
        _stack.reset(token)
    except ValueError:  # token created in another context (generator resumed elsewhere)
        _stack.set(_stack.get()[:-1])


def current_observation() -> Observation | None:
    stack = _stack.get()
    return stack[-1] if stack else None


def current_trace_id() -> str | None:
    obs = current_observation()
    return obs.trace_id if obs is not None else None
