"""Playground run engine: render variables, call the adapter, accumulate usage, cost it.

`Run.events()` yields `Event`s (delta, tool_call, usage, done, error). All state is local to the
run, so concurrent runs never share usage. Cost uses the Phase 1 registry (`cost_source` is
"calculated"). With `trace=True` the run is ingested through the normal pipeline as a trace in the
`playground` environment. The provider stream is closed when the run finishes, fails or is
cancelled, so a client that goes away does not leave the SDK connection open.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from traceloom.cost.pricing import apply_cost
from traceloom.ingest.accept import IngestLimits, accept_batch
from traceloom.llm.prefill import tools_from
from traceloom.llm.providers import (
    DoneDelta,
    ErrorDelta,
    Provider,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
    scrub,
)
from traceloom.ports import ObservationRow
from traceloom.prompts.variables import compile_messages

log = logging.getLogger("traceloom.playground")

ENVIRONMENT = "playground"
COST_USAGE_KEYS = ("input", "output", "cache_read", "cache_write")


@dataclass(frozen=True)
class Event:
    name: str  # delta | tool_call | usage | done | error
    data: dict[str, Any]


@dataclass
class RunSpec:
    messages: list[dict[str, Any]]
    model: str
    params: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    response_schema: dict[str, Any] | None = None
    variables: dict[str, str] = field(default_factory=dict)
    trace: bool = False
    prompt_name: str | None = None
    prompt_version: int | None = None
    secrets: Sequence[str] = ()  # extra values to scrub from error text


class Run:
    def __init__(self, container: Any, project_id: str, provider: Provider, spec: RunSpec):
        self.container, self.project_id = container, project_id
        self.provider, self.spec = provider, spec
        self._cancelled = threading.Event()
        self._events: Iterator[Event] | None = None
        self._running = threading.Lock()

    # --- control ----------------------------------------------------------------------------

    def cancel(self) -> None:
        """Ask the run to stop at its next delta; closes it at once when it is idle."""
        self._cancelled.set()
        if self._events is not None and self._running.acquire(blocking=False):
            try:
                self._events.close()  # type: ignore[attr-defined]
            finally:
                self._running.release()

    def __iter__(self) -> Iterator[Event]:
        return self.events()

    def events(self) -> Iterator[Event]:
        if self._events is None:
            self._events = self._run()
        return self._guarded(self._events)

    def _guarded(self, events: Iterator[Event]) -> Iterator[Event]:
        # The lock tells cancel() whether the generator is mid-step in a worker thread.
        while True:
            with self._running:
                try:
                    event = next(events)
                except StopIteration:
                    return
            yield event

    # --- the run ----------------------------------------------------------------------------

    def _run(self) -> Iterator[Event]:
        spec = self.spec
        started_wall = datetime.now(UTC)
        started = time.monotonic()
        first_token: float | None = None
        text: list[str] = []
        calls: list[dict[str, Any]] = []
        usage = {"input": 0, "output": 0}
        extra: dict[str, int] = {}
        finish: str | None = None
        errors: list[str] = []
        messages = compile_messages(spec.messages, spec.variables).messages
        stream = self.provider.stream(
            messages, spec.model, spec.params, tools_from(spec.tools), spec.response_schema
        )
        try:
            for delta in stream:
                if self._cancelled.is_set():
                    finish = "cancelled"
                    break
                if isinstance(delta, TextDelta):
                    if first_token is None:
                        first_token = time.monotonic()
                    text.append(delta.text)
                    yield Event("delta", {"text": delta.text})
                elif isinstance(delta, ToolCallDelta):
                    if first_token is None:
                        first_token = time.monotonic()
                    call = {"id": delta.id, "name": delta.name, "arguments": delta.arguments}
                    calls.append(call)
                    yield Event("tool_call", call)
                elif isinstance(delta, UsageDelta):
                    usage["input"] += delta.input_tokens
                    usage["output"] += delta.output_tokens
                    for k, v in delta.details.items():
                        extra[k] = extra.get(k, 0) + v
                elif isinstance(delta, ErrorDelta):
                    message = scrub(delta.message, self.spec.secrets)
                    errors.append(message)
                    yield Event("error", {"message": message})
                elif isinstance(delta, DoneDelta):
                    finish = delta.finish_reason or finish
        except GeneratorExit:
            self._cancelled.set()
            raise
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()  # releases the provider SDK's connection
        ended = time.monotonic()
        if self._cancelled.is_set() and finish == "cancelled":
            return  # the client is gone; nothing to report to
        cost = self._cost(usage, extra, started_wall)
        yield Event(
            "usage",
            {
                "input_tokens": usage["input"],
                "output_tokens": usage["output"],
                "total_tokens": usage["input"] + usage["output"],
                "details": extra,
                "cost": cost["total"] if cost else None,
                "cost_details": cost,
                "cost_source": "calculated" if cost else "none",
                "latency_ms": round((ended - started) * 1000, 1),
                "ttft_ms": None
                if first_token is None
                else round((first_token - started) * 1000, 1),
            },
        )
        done: dict[str, Any] = {"finish_reason": finish or ("error" if errors else None)}
        if spec.trace:
            trace_id = self._ingest(messages, text, calls, errors, usage, extra, started_wall)
            if trace_id:
                done["trace_id"] = trace_id
        yield Event("done", done)

    # --- cost and tracing ---------------------------------------------------------------------

    def _usage_details(self, usage: Mapping[str, int], extra: Mapping[str, int]) -> dict[str, int]:
        details = {"input": usage["input"], "output": usage["output"]}
        details.update({k: v for k, v in extra.items() if k in COST_USAGE_KEYS and v})
        details["total"] = usage["input"] + usage["output"]
        return details

    def _cost(self, usage, extra, at: datetime) -> dict[str, float]:
        if not (usage["input"] or usage["output"]):
            return {}
        obs = ObservationRow(
            id="playground",
            trace_id="playground",
            start_time=at,
            model=self.spec.model,
            usage_details=self._usage_details(usage, extra),
            usage_source="provided",
        )
        try:
            apply_cost(obs, self.project_id, self.container.models)
        except Exception:
            log.exception("could not cost a playground run")
            return {}
        return dict(obs.cost_details) if obs.cost_source == "calculated" else {}

    def _ingest(self, messages, text, calls, errors, usage, extra, started: datetime):
        """Send the run through the normal ingestion path. Never raises."""
        spec, now = self.spec, datetime.now(UTC)
        trace_id, obs_id = uuid.uuid4().hex, uuid.uuid4().hex
        output: Any = "".join(text)
        if calls:
            output = {"content": output, "tool_calls": calls}
        body: dict[str, Any] = {
            "id": obs_id,
            "traceId": trace_id,
            "name": "playground",
            "model": spec.model,
            "modelParameters": spec.params,
            "input": messages,
            "output": output,
            "startTime": started.isoformat(),
            "endTime": now.isoformat(),
            "usageDetails": self._usage_details(usage, extra),
            "environment": ENVIRONMENT,
        }
        if errors:
            body.update(level="ERROR", statusMessage=errors[0])
        if spec.prompt_name:
            body["promptName"] = spec.prompt_name
            if spec.prompt_version is not None:
                body["promptVersion"] = spec.prompt_version
        batch = [
            {
                "id": uuid.uuid4().hex,
                "type": "trace-create",
                "timestamp": started.isoformat(),
                "body": {
                    "id": trace_id,
                    "name": "playground run",
                    "environment": ENVIRONMENT,
                    "input": messages,
                    "output": output,
                },
            },
            {
                "id": uuid.uuid4().hex,
                "type": "generation-create",
                "timestamp": started.isoformat(),
                "body": body,
            },
        ]
        c, s = self.container, self.container.settings
        try:
            accept_batch(
                self.project_id,
                json.dumps({"batch": batch}).encode(),
                c.blob_store,
                c.queue,
                IngestLimits(s.ingest_max_events, s.ingest_max_bytes),
            )
        except Exception:
            log.exception("could not ingest a playground trace")
            return None
        return trace_id
