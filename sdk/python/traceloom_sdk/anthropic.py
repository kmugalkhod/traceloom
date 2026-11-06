"""Anthropic integration: `wrap(client)` traces `messages.create` and `messages.stream`.

Works with Anthropic and AsyncAnthropic clients, streaming or not. The client is patched in place
and returned. The anthropic package is not imported here.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Dict, Optional, Tuple

from traceloom_sdk._llm import (
    Accumulator,
    AsyncTracedStream,
    Run,
    TracedStream,
    _finish_stream,
    get,
    plain,
    request_params,
    wrap_method,
)
from traceloom_sdk.client import Traceloom

log = logging.getLogger("traceloom")

_EXCLUDE = {"messages", "model", "stream", "system"}


def _usage(usage: Any, previous: Optional[Dict[str, int]] = None) -> Dict[str, int]:
    if usage is None:
        return previous or {}
    merged = dict(previous or {})
    raw = {
        "input": get(usage, "input_tokens"),
        "output": get(usage, "output_tokens"),
        "cache_read": get(usage, "cache_read_input_tokens"),
        "cache_write": get(usage, "cache_creation_input_tokens"),
    }
    merged.update({k: int(v) for k, v in raw.items() if isinstance(v, (int, float))})
    merged["total"] = sum(v for k, v in merged.items() if k != "total")
    return {k: v for k, v in merged.items() if v}


def _output(content: Any) -> Any:
    blocks = plain(content) or []
    if (
        isinstance(blocks, list)
        and blocks
        and all(isinstance(b, dict) and b.get("type") == "text" for b in blocks)
    ):
        return "".join(b.get("text", "") for b in blocks)
    return blocks


def _extract(message: Any) -> Tuple[Any, Dict[str, int], Any]:
    return _output(get(message, "content")), _usage(get(message, "usage")), get(message, "model")


class _Accumulator(Accumulator):
    """Folds Messages API stream events."""

    def __init__(self) -> None:
        super().__init__()
        self.blocks: Dict[int, Dict[str, Any]] = {}
        self.stop_reason: Any = None

    def feed(self, event: Any) -> bool:
        kind = get(event, "type", default="")
        first = False
        if kind == "message_start":
            message = get(event, "message")
            self.model = get(message, "model") or self.model
            self.usage = _usage(get(message, "usage"), self.usage)
        elif kind == "content_block_start":
            block = plain(get(event, "content_block")) or {}
            self.blocks[get(event, "index", default=0)] = dict(block)
        elif kind == "content_block_delta":
            slot = self.blocks.setdefault(get(event, "index", default=0), {"type": "text"})
            delta = get(event, "delta")
            dtype = get(delta, "type", default="")
            if dtype == "text_delta":
                slot["text"] = slot.get("text", "") + get(delta, "text", default="")
                first = True
            elif dtype == "input_json_delta":
                slot["_json"] = slot.get("_json", "") + get(delta, "partial_json", default="")
                first = True
            elif dtype == "thinking_delta":
                slot["thinking"] = slot.get("thinking", "") + get(delta, "thinking", default="")
                first = True
        elif kind == "message_delta":
            self.usage = _usage(get(event, "usage"), self.usage)
            self.stop_reason = get(get(event, "delta"), "stop_reason") or self.stop_reason
        return first

    def output(self) -> Any:
        blocks = []
        for index in sorted(self.blocks):
            block = dict(self.blocks[index])
            raw = block.pop("_json", None)
            if raw is not None:
                block["input_json"] = raw
            blocks.append(block)
        return _output(blocks)


def _begin_for(traceloom: Optional[Traceloom]):
    def begin(kwargs: Dict[str, Any]) -> Run:
        params = request_params(kwargs, _EXCLUDE)
        if kwargs.get("system"):
            params["system"] = plain(kwargs["system"])
        return Run.begin(
            traceloom,
            "anthropic-messages",
            kwargs.get("model"),
            plain(kwargs.get("messages")),
            params,
        )

    return begin


class _SyncStream(TracedStream):
    @property
    def text_stream(self) -> Any:
        for text in self._inner.text_stream:
            self._run.first_token()
            yield text


class _AsyncStream(AsyncTracedStream):
    @property
    def text_stream(self) -> Any:
        async def gen() -> Any:
            async for text in self._inner.text_stream:
                self._run.first_token()
                yield text

        return gen()


class _StreamManagerProxy:
    """Wraps the object returned by `messages.stream(...)` (a sync or async context manager)."""

    def __init__(self, manager: Any, run: Run, acc: _Accumulator, is_async: bool) -> None:
        self._manager, self._run, self._acc, self._async = manager, run, acc, is_async
        self._stream: Any = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)

    def _wrap_stream(self, stream: Any) -> Any:
        self._stream = stream
        return (_AsyncStream if self._async else _SyncStream)(stream, self._run, self._acc)

    def _finish_from(self, message: Any, exc: Any) -> None:
        if exc is not None or self._acc.blocks or message is None:
            _finish_stream(self._run, self._acc, exc)
        else:  # consumed through text_stream: take the assembled message
            self._run.finish(*_extract(message))

    def __enter__(self) -> Any:
        try:
            return self._wrap_stream(self._manager.__enter__())
        except BaseException as exc:
            self._run.fail(exc)
            raise

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        message = None
        try:
            if exc is None and not self._acc.blocks and self._stream is not None:
                message = self._stream.get_final_message()
        except Exception:
            log.exception("Traceloom: could not read the final message")
        self._finish_from(message, exc)
        return self._manager.__exit__(exc_type, exc, tb)

    async def __aenter__(self) -> Any:
        try:
            return self._wrap_stream(await self._manager.__aenter__())
        except BaseException as exc:
            self._run.fail(exc)
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        message = None
        try:
            if exc is None and not self._acc.blocks and self._stream is not None:
                message = await self._stream.get_final_message()
        except Exception:
            log.exception("Traceloom: could not read the final message")
        self._finish_from(message, exc)
        return await self._manager.__aexit__(exc_type, exc, tb)


def _wrap_stream_method(messages: Any, begin: Any) -> None:
    original = messages.stream
    if getattr(original, "_traceloom_wrapped", False):
        return

    @functools.wraps(original)
    def traced(*args: Any, **kwargs: Any) -> Any:
        try:
            run = begin(kwargs)
        except Exception:
            log.exception("Traceloom: could not start generation")
            return original(*args, **kwargs)
        try:
            manager = original(*args, **kwargs)
        except BaseException as exc:
            run.fail(exc)
            raise
        try:
            return _StreamManagerProxy(manager, run, _Accumulator(), hasattr(manager, "__aenter__"))
        except Exception:
            log.exception("Traceloom: could not wrap the stream")
            return manager

    traced._traceloom_wrapped = True  # type: ignore[attr-defined]
    messages.stream = traced


def wrap(client: Any, traceloom: Optional[Traceloom] = None) -> Any:
    """Trace calls made through `client`. Returns the same client."""
    begin = _begin_for(traceloom)
    try:
        messages = client.messages
        wrap_method(messages, "create", begin, _extract, _Accumulator)
        if hasattr(messages, "stream"):
            _wrap_stream_method(messages, begin)
    except Exception:
        log.warning("Traceloom: could not instrument messages", exc_info=True)
    return client
