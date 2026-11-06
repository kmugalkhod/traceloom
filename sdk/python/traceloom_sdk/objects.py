"""Observation handles (Span, Generation, Event) and the helpers behind the manual API."""

from __future__ import annotations

import datetime as _dt
import functools
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from traceloom_sdk import context
from traceloom_sdk.client import new_id, utc_iso

if TYPE_CHECKING:
    from traceloom_sdk.client import Traceloom

log = logging.getLogger("traceloom")

# python keyword -> ingestion body key
_BODY_KEYS = {
    "input": "input",
    "output": "output",
    "metadata": "metadata",
    "level": "level",
    "status_message": "statusMessage",
    "version": "version",
    "environment": "environment",
    "model": "model",
    "model_parameters": "modelParameters",
    "usage_details": "usageDetails",
    "cost_details": "costDetails",
    "prompt_name": "promptName",
    "prompt_version": "promptVersion",
    "completion_start_time": "completionStartTime",
    "start_time": "startTime",
    "end_time": "endTime",
}
_ALIASES = {"usage": "usage_details", "cost": "cost_details", "model_params": "model_parameters"}
_TRACE_KEYS = {
    "name": "name",
    "user_id": "userId",
    "session_id": "sessionId",
    "tags": "tags",
    "metadata": "metadata",
    "release": "release",
    "version": "version",
    "environment": "environment",
    "public": "public",
    "input": "input",
    "output": "output",
}
_UPDATE_TYPE = {"span": "span-update", "generation": "generation-update"}
_CREATE_TYPE = {"span": "span-create", "generation": "generation-create", "event": "event-create"}


def _time(value: Any) -> Any:
    if isinstance(value, _dt.datetime):
        return utc_iso(value)
    if isinstance(value, (int, float)):  # epoch seconds
        return utc_iso(_dt.datetime.fromtimestamp(value, _dt.UTC))
    return value


def prompt_link(prompt: Any) -> dict[str, Any]:
    """promptName/promptVersion for a fetched prompt. A fallback or anything unusable links
    nothing, and this never raises."""
    try:
        name = getattr(prompt, "name", None)
        version = getattr(prompt, "version", None)
        if prompt is None or getattr(prompt, "is_fallback", False) or not name or not version:
            return {}
        return {"promptName": str(name), "promptVersion": int(version)}
    except Exception:
        log.debug("Traceloom: could not link prompt", exc_info=True)
        return {}


def _body(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key == "prompt":  # a TextPrompt/ChatPrompt from client.get_prompt
            out.update(prompt_link(value))
            continue
        key = _ALIASES.get(key, key)
        target = _BODY_KEYS.get(key)
        if target is None:
            log.warning("ignoring unknown observation field %r", key)
            continue
        out[target] = _time(value) if key.endswith("_time") else value
    return out


def _safe(fn: Callable) -> Callable:
    """Public entry points never raise into user code; failures go to the traceloom logger."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            result = fn(*args, **kwargs)
        except Exception:
            log.exception("Traceloom: %s failed", fn.__name__)
            return args[0] if args and isinstance(args[0], Observation) else None
        return result

    return wrapper


class Observation:
    """A started observation. Use it as a context manager, or call `.end()`."""

    def __init__(
        self,
        client: Traceloom | None,
        as_type: str,
        name: str | None,
        trace_id: str,
        parent_id: str | None,
        is_root: bool,
        obs_id: str | None = None,
    ) -> None:
        self._client = client
        self.type = as_type
        self.name = name
        self.id = obs_id or new_id()
        self.trace_id = trace_id
        self.parent_id = parent_id
        self._is_root = is_root
        self._pending: dict[str, Any] = {}
        self._ended = False
        self._tokens: list = []

    @_safe
    def update(self, **fields: Any) -> Observation:
        """Set fields to be sent when the observation ends."""
        self._pending.update(_body(fields))
        return self

    @_safe
    def update_trace(self, **fields: Any) -> None:
        update_current_trace(self._client, fields, trace_id=self.trace_id)

    @_safe
    def end(self, **fields: Any) -> None:
        if self._ended:
            return
        self._ended = True
        self._pending.update(_body(fields))
        client = self._client
        if client is None:
            return
        self._pending.setdefault("endTime", utc_iso())
        body = {"id": self.id, "traceId": self.trace_id, "type": self.type, **self._pending}
        client.emit(_UPDATE_TYPE.get(self.type, "observation-update"), body, self.trace_id)
        if self._is_root and "output" in self._pending:
            client.emit(
                "trace-create",
                {"id": self.trace_id, "output": self._pending["output"]},
                self.trace_id,
            )

    def start_observation(
        self, name: str | None = None, as_type: str = "span", **fields: Any
    ) -> Observation:
        return start(self._client, as_type, name, fields, trace_id=self.trace_id, parent_id=self.id)

    def start_span(self, name: str | None = None, **fields: Any) -> Observation:
        return self.start_observation(name, "span", **fields)

    def start_generation(self, name: str | None = None, **fields: Any) -> Observation:
        return self.start_observation(name, "generation", **fields)

    def event(self, name: str | None = None, **fields: Any) -> Observation:
        return self.start_observation(name, "event", **fields)

    @_safe
    def score(self, name: str, value: Any, **kwargs: Any) -> None:
        if self._client is not None:
            score(self._client, name, value, self.trace_id, self.id, **kwargs)

    def __enter__(self) -> Observation:
        try:
            self._tokens.append(context.push(self))
        except Exception:
            log.exception("Traceloom: failed to enter observation context")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if exc is not None:
                mark_error(self, exc)
            self.end()
        except Exception:
            log.exception("Traceloom: failed to close observation")
        finally:
            if self._tokens:
                try:
                    context.pop(self._tokens.pop())
                except Exception:
                    pass
        return False  # never swallow the user's exception


class Span(Observation):
    pass


class Generation(Observation):
    pass


class Event(Observation):
    pass


_CLASSES = {"generation": Generation, "event": Event}


def mark_error(obs: Observation, exc: BaseException) -> None:
    if isinstance(exc, Exception):
        obs.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
    else:  # cancellation, KeyboardInterrupt: interrupted rather than failed
        obs.update(level="WARNING", status_message=f"interrupted: {type(exc).__name__}")


def _noop(as_type: str) -> Observation:
    return _CLASSES.get(as_type, Span)(None, as_type, None, new_id(), None, False)


def start(
    client: Traceloom | None,
    as_type: str,
    name: str | None,
    fields: dict[str, Any],
    trace_id: str | None = None,
    parent_id: str | None = None,
) -> Observation:
    """Create an observation (and its trace when it has no parent) and emit its create event."""
    try:
        if client is None:
            return _noop(as_type)
        if trace_id is None and parent_id is None:
            parent = context.current_observation()
            if parent is not None:
                trace_id, parent_id = parent.trace_id, parent.id
        is_root = trace_id is None
        trace_id = trace_id or new_id()
        fields = dict(fields)
        obs = _CLASSES.get(as_type, Span)(
            client, as_type, name, trace_id, parent_id, is_root, fields.pop("id", None)
        )
        body: dict[str, Any] = {
            "id": obs.id,
            "traceId": trace_id,
            "parentObservationId": parent_id,
            "type": as_type,
            "name": name,
            "startTime": utc_iso(),
        }
        body.update(_body(fields))
        if is_root:
            client.emit(
                "trace-create",
                {
                    "id": trace_id,
                    "name": name,
                    "timestamp": body["startTime"],
                    "input": body.get("input"),
                    "environment": client.environment,
                    "release": client.release,
                    "version": client.version,
                },
                trace_id,
            )
        client.emit(_CREATE_TYPE.get(as_type, "observation-create"), body, trace_id)
        if as_type == "event":
            obs._ended = True  # an event is a point in time: no update follows
        return obs
    except Exception:
        log.exception("Traceloom: failed to start observation")
        return _noop(as_type)


def update_current_trace(
    client: Traceloom | None, fields: dict[str, Any], trace_id: str | None = None
) -> None:
    try:
        trace_id = trace_id or context.current_trace_id()
        if client is None or trace_id is None:
            log.warning("update_current_trace called outside an observation; ignored")
            return
        body: dict[str, Any] = {"id": trace_id}
        for key, value in fields.items():
            target = _TRACE_KEYS.get(key)
            if target is None:
                log.warning("ignoring unknown trace field %r", key)
            else:
                body[target] = value
        client.emit("trace-create", body, trace_id)
    except Exception:
        log.exception("Traceloom: update_current_trace failed")


def score(
    client: Traceloom | None,
    name: str,
    value: Any,
    trace_id: str | None = None,
    observation_id: str | None = None,
    data_type: str | None = None,
    comment: str | None = None,
) -> None:
    try:
        if client is None:
            return
        if trace_id is None:
            current = context.current_observation()
            if current is None:
                log.warning("score() needs a trace_id outside an observation; ignored")
                return
            trace_id = current.trace_id
            observation_id = observation_id or current.id
        if data_type is None:
            if isinstance(value, bool):
                data_type = "BOOLEAN"
            elif isinstance(value, str):
                data_type = "CATEGORICAL"
            else:
                data_type = "NUMERIC"
        client.emit(
            "score-create",
            {
                "id": new_id(),
                "traceId": trace_id,
                "observationId": observation_id,
                "name": name,
                "value": value,
                "dataType": data_type,
                "comment": comment,
            },
            trace_id,
        )
    except Exception:
        log.exception("Traceloom: score failed")
