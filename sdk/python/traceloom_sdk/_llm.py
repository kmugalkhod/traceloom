"""Shared plumbing for the provider wrappers: a generation run, stream proxies, helpers.

Everything here is defensive: instrumentation errors are logged to the `traceloom` logger and
never reach the caller, and the provider's own return values and exceptions pass through.
"""

from __future__ import annotations

import datetime as _dt
import functools
import inspect
import logging
from typing import Any, Callable, Dict, Optional

from traceloom_sdk import objects
from traceloom_sdk.client import Traceloom, get_client

log = logging.getLogger("traceloom")


def get(obj: Any, *path: str, default: Any = None) -> Any:
    """obj.a.b or obj["a"]["b"], whichever the object supports; `default` when missing/None."""
    for key in path:
        if obj is None:
            return default
        obj = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
    return default if obj is None else obj


def plain(value: Any) -> Any:
    """A JSON-friendly copy of an SDK response object, without unset (None) fields."""
    for attr in ("model_dump", "to_dict", "dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                return plain(method())
            except Exception:
                break
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items() if v is not None}
    return value


def usage_dict(**counts: Any) -> Dict[str, int]:
    return {k: int(v) for k, v in counts.items() if isinstance(v, (int, float)) and v}


class Run:
    """One traced model call."""

    def __init__(self, gen: Optional[objects.Observation]) -> None:
        self.gen = gen
        self._first_token = False
        self._done = False

    @classmethod
    def begin(
        cls,
        client: Optional[Traceloom],
        name: str,
        model: Any,
        input: Any,
        params: Dict[str, Any],
    ) -> "Run":
        try:
            fields: Dict[str, Any] = {"input": input, "model_parameters": params}
            if model:
                fields["model"] = str(model)
            return cls((client or get_client()).start_generation(name, **fields))
        except Exception:
            log.exception("Traceloom: could not start generation")
            return cls(None)

    def first_token(self) -> None:
        if self._first_token or self.gen is None:
            return
        self._first_token = True
        try:
            self.gen.update(completion_start_time=_dt.datetime.now(_dt.timezone.utc))
        except Exception:
            log.exception("Traceloom: could not record time to first token")

    def finish(
        self, output: Any = None, usage: Optional[Dict[str, int]] = None, model: Any = None
    ) -> None:
        if self._done or self.gen is None:
            return
        self._done = True
        try:
            fields: Dict[str, Any] = {}
            if output is not None:
                fields["output"] = output
            if usage:
                fields["usage_details"] = usage
            if model:
                fields["model"] = str(model)
            self.gen.end(**fields)
        except Exception:
            log.exception("Traceloom: could not finish generation")

    def fail(self, exc: BaseException) -> None:
        if self._done or self.gen is None:
            return
        self._done = True
        try:
            objects.mark_error(self.gen, exc)
            self.gen.end()
        except Exception:
            log.exception("Traceloom: could not record provider error")


class Accumulator:
    """Folds stream chunks into (output, usage, model). Subclasses implement `feed`."""

    model: Any = None
    usage: Dict[str, int]

    def __init__(self) -> None:
        self.usage = {}

    def feed(self, chunk: Any) -> bool:
        """Consume a chunk; return True when it carries the first generated content."""
        raise NotImplementedError

    def output(self) -> Any:
        raise NotImplementedError


def _feed(run: Run, acc: Accumulator, chunk: Any) -> None:
    try:
        if acc.feed(chunk):
            run.first_token()
    except Exception:
        log.exception("Traceloom: could not read a stream chunk")


def _finish_stream(run: Run, acc: Accumulator, exc: Optional[BaseException] = None) -> None:
    if exc is not None and not isinstance(exc, (StopIteration, StopAsyncIteration)):
        run.fail(exc)
        return
    try:
        run.finish(acc.output(), acc.usage, acc.model)
    except Exception:
        log.exception("Traceloom: could not assemble stream output")
        run.finish()


class TracedStream:
    """Transparent proxy over a sync provider stream. Iterating records TTFT, output and usage."""

    def __init__(self, inner: Any, run: Run, acc: Accumulator) -> None:
        self._inner, self._run, self._acc = inner, run, acc
        self._iterator: Any = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> Any:
        if self._iterator is None:
            self._iterator = iter(self._inner)
        try:
            chunk = next(self._iterator)
        except StopIteration:
            _finish_stream(self._run, self._acc)
            raise
        except BaseException as exc:
            _finish_stream(self._run, self._acc, exc)
            raise
        _feed(self._run, self._acc, chunk)
        return chunk

    def __enter__(self) -> "TracedStream":
        enter = getattr(self._inner, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        _finish_stream(self._run, self._acc, exc)
        exit_ = getattr(self._inner, "__exit__", None)
        return exit_(exc_type, exc, tb) if exit_ is not None else None

    def close(self) -> Any:
        _finish_stream(self._run, self._acc)
        return self._inner.close()


class AsyncTracedStream:
    """Async twin of TracedStream."""

    def __init__(self, inner: Any, run: Run, acc: Accumulator) -> None:
        self._inner, self._run, self._acc = inner, run, acc
        self._iterator: Any = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> Any:
        if self._iterator is None:
            self._iterator = self._inner.__aiter__()
        try:
            chunk = await self._iterator.__anext__()
        except StopAsyncIteration:
            _finish_stream(self._run, self._acc)
            raise
        except BaseException as exc:
            _finish_stream(self._run, self._acc, exc)
            raise
        _feed(self._run, self._acc, chunk)
        return chunk

    async def __aenter__(self) -> "AsyncTracedStream":
        enter = getattr(self._inner, "__aenter__", None)
        if enter is not None:
            await enter()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        _finish_stream(self._run, self._acc, exc)
        exit_ = getattr(self._inner, "__aexit__", None)
        return await exit_(exc_type, exc, tb) if exit_ is not None else None

    async def close(self) -> Any:
        _finish_stream(self._run, self._acc)
        return await self._inner.close()


def wrap_method(
    owner: Any,
    attr: str,
    begin: Callable[[Dict[str, Any]], Run],
    extract: Callable[[Any], Any],
    new_accumulator: Callable[[], Accumulator],
) -> None:
    """Replace `owner.attr` with a traced version.

    `begin(kwargs)` starts the run; `extract(response)` returns (output, usage, model);
    `new_accumulator()` builds the stream folder. Return values and exceptions are the provider's.
    """
    original = getattr(owner, attr)
    if getattr(original, "_traceloom_wrapped", False):
        return

    def start(kwargs: Dict[str, Any]) -> Run:
        try:
            return begin(kwargs)
        except Exception:
            log.exception("Traceloom: could not start generation")
            return Run(None)

    def complete(run: Run, result: Any) -> Any:
        try:
            if getattr(result, "_traceloom_skip", False):
                return result
            output, usage, model = extract(result)
            run.finish(output, usage, model)
        except Exception:
            log.exception("Traceloom: could not read the provider response")
            run.finish()
        return result

    def stream(run: Run, result: Any) -> Any:
        try:
            if hasattr(result, "__aiter__"):
                return AsyncTracedStream(result, run, new_accumulator())
            return TracedStream(result, run, new_accumulator())
        except Exception:
            log.exception("Traceloom: could not wrap the stream")
            return result

    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            run = start(kwargs)
            try:
                result = await original(*args, **kwargs)
            except BaseException as exc:
                run.fail(exc)
                raise
            return stream(run, result) if kwargs.get("stream") else complete(run, result)

        async_wrapper._traceloom_wrapped = True  # type: ignore[attr-defined]
        setattr(owner, attr, async_wrapper)
    else:

        @functools.wraps(original)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            run = start(kwargs)
            try:
                result = original(*args, **kwargs)
            except BaseException as exc:
                run.fail(exc)
                raise
            if inspect.isawaitable(
                result
            ):  # decorated async method: a plain def returning a coroutine
                return _await_and_finish(run, result, complete, stream, bool(kwargs.get("stream")))
            if kwargs.get("stream"):
                return stream(run, result)
            return complete(run, result)

        sync_wrapper._traceloom_wrapped = True  # type: ignore[attr-defined]
        setattr(owner, attr, sync_wrapper)


async def _await_and_finish(
    run: Run, awaitable: Any, complete: Callable, stream: Callable, streaming: bool
) -> Any:
    try:
        result = await awaitable
    except BaseException as exc:
        run.fail(exc)
        raise
    return stream(run, result) if streaming else complete(run, result)


def request_params(kwargs: Dict[str, Any], exclude: Any) -> Dict[str, Any]:
    return {
        k: v
        for k, v in kwargs.items()
        if k not in exclude and v is not None and not k.startswith("extra_") and k != "timeout"
    }
