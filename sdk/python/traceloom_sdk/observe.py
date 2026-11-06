"""The @observe decorator for sync/async functions and sync/async generators."""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any

from traceloom_sdk import context, objects
from traceloom_sdk.client import get_client

log = logging.getLogger("traceloom")


def _capture_input(fn: Callable, args: tuple, kwargs: dict) -> Any:
    try:
        bound = inspect.signature(fn).bind(*args, **kwargs)
        bound.apply_defaults()
        data: dict[str, Any] = dict(bound.arguments)
        for key in ("self", "cls"):
            data.pop(key, None)
        return data
    except Exception:
        return {"args": list(args), "kwargs": kwargs}


def _begin(fn: Callable, name: str | None, as_type: str, capture_input: bool, a: tuple, kw: dict):
    try:
        fields: dict[str, Any] = {}
        if capture_input:
            fields["input"] = _capture_input(fn, a, kw)
        return objects.start(get_client(), as_type, name or fn.__name__, fields)
    except Exception:
        log.exception("Traceloom: @observe failed to start")
        return objects._noop(as_type)


def _finish(obs: objects.Observation, output: Any, exc: BaseException | None, capture: bool):
    try:
        if exc is not None:
            objects.mark_error(obs, exc)
        if capture and output is not None and exc is None:
            obs.update(output=output)
        obs.end()
    except Exception:
        log.exception("Traceloom: @observe failed to finish")


def _enter(obs: objects.Observation):
    try:
        return context.push(obs)
    except Exception:
        return None


def _leave(token: Any) -> None:
    if token is not None:
        try:
            context.pop(token)
        except Exception:
            pass


def observe(
    func: Callable | None = None,
    *,
    name: str | None = None,
    as_type: str = "span",
    capture_input: bool = True,
    capture_output: bool = True,
) -> Any:
    """Trace a function. Usable as `@observe` or `@observe(name=..., as_type=...)`."""

    def decorate(fn: Callable) -> Callable:
        opts = (name, as_type, capture_input, capture_output)
        if inspect.isasyncgenfunction(fn):
            return _wrap_async_gen(fn, *opts)
        if inspect.isgeneratorfunction(fn):
            return _wrap_gen(fn, *opts)
        if inspect.iscoroutinefunction(fn):
            return _wrap_async(fn, *opts)
        return _wrap_sync(fn, *opts)

    return decorate(func) if func is not None else decorate


def _wrap_sync(fn, name, as_type, cap_in, cap_out):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        obs = _begin(fn, name, as_type, cap_in, args, kwargs)
        token = _enter(obs)
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            _leave(token)
            _finish(obs, None, exc, cap_out)
            raise
        _leave(token)
        _finish(obs, result, None, cap_out)
        return result

    return wrapper


def _wrap_async(fn, name, as_type, cap_in, cap_out):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        obs = _begin(fn, name, as_type, cap_in, args, kwargs)
        token = _enter(obs)
        try:
            result = await fn(*args, **kwargs)
        except BaseException as exc:
            _leave(token)
            _finish(obs, None, exc, cap_out)
            raise
        _leave(token)
        _finish(obs, result, None, cap_out)
        return result

    return wrapper


def _wrap_gen(fn, name, as_type, cap_in, cap_out):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        obs = _begin(fn, name, as_type, cap_in, args, kwargs)
        items: list[Any] = []
        gen = fn(*args, **kwargs)
        to_send: Any = None
        to_throw: BaseException | None = None
        try:
            while True:
                # The observation is current only while the generator body runs, not while the
                # consumer holds the yielded item.
                token = _enter(obs)
                try:
                    if to_throw is not None:
                        pending, to_throw = to_throw, None
                        item = gen.throw(pending)
                    else:
                        item = gen.send(to_send)
                except StopIteration as stop:
                    _leave(token)
                    _finish(obs, items, None, cap_out)
                    return stop.value
                except BaseException:
                    _leave(token)
                    raise
                _leave(token)
                items.append(item)
                try:
                    to_send = yield item
                except GeneratorExit:
                    gen.close()
                    _finish(obs, items, None, cap_out)
                    raise
                except BaseException as thrown:
                    to_throw = thrown
        except BaseException as exc:
            if not isinstance(exc, GeneratorExit):
                _finish(obs, None, exc, cap_out)
            raise

    return wrapper


def _wrap_async_gen(fn, name, as_type, cap_in, cap_out):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        obs = _begin(fn, name, as_type, cap_in, args, kwargs)
        items: list[Any] = []
        gen = fn(*args, **kwargs)
        to_send: Any = None
        to_throw: BaseException | None = None
        try:
            while True:
                token = _enter(obs)
                try:
                    if to_throw is not None:
                        pending, to_throw = to_throw, None
                        item = await gen.athrow(pending)
                    else:
                        item = await gen.asend(to_send)
                except StopAsyncIteration:
                    _leave(token)
                    _finish(obs, items, None, cap_out)
                    return
                except BaseException:
                    _leave(token)
                    raise
                _leave(token)
                items.append(item)
                try:
                    to_send = yield item
                except GeneratorExit:
                    await gen.aclose()
                    _finish(obs, items, None, cap_out)
                    raise
                except BaseException as thrown:
                    to_throw = thrown
        except BaseException as exc:
            if not isinstance(exc, GeneratorExit):
                _finish(obs, None, exc, cap_out)
            raise

    return wrapper
