"""OpenAI integration: `wrap(client)` traces chat completions and the Responses API.

Works with OpenAI and AsyncOpenAI clients (and compatible ones), streaming or not. The client is
patched in place and returned. The openai package is not imported here.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from traceloom_sdk._llm import Accumulator, Run, get, plain, request_params, usage_dict, wrap_method
from traceloom_sdk.client import Traceloom

log = logging.getLogger("traceloom")

_CHAT_EXCLUDE = {"messages", "model", "stream"}
_RESPONSES_EXCLUDE = {"input", "model", "stream", "instructions"}


def _chat_usage(usage: Any) -> Dict[str, int]:
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


def _responses_usage(usage: Any) -> Dict[str, int]:
    if usage is None:
        return {}
    prompt = get(usage, "input_tokens", default=0)
    cached = get(usage, "input_tokens_details", "cached_tokens", default=0)
    return usage_dict(
        input=max(prompt - cached, 0),
        output=get(usage, "output_tokens", default=0),
        total=get(usage, "total_tokens", default=0),
        cache_read=cached,
    )


def _chat_extract(response: Any) -> Tuple[Any, Dict[str, int], Any]:
    choices = get(response, "choices", default=[]) or []
    messages = [plain(get(c, "message")) for c in choices]
    output = messages[0] if len(messages) == 1 else (messages or None)
    return output, _chat_usage(get(response, "usage")), get(response, "model")


def _responses_extract(response: Any) -> Tuple[Any, Dict[str, int], Any]:
    output = get(response, "output_text") or plain(get(response, "output"))
    return output, _responses_usage(get(response, "usage")), get(response, "model")


class _ChatAccumulator(Accumulator):
    def __init__(self) -> None:
        super().__init__()
        self.text = []
        self.tool_calls: Dict[int, Dict[str, Any]] = {}
        self.role = "assistant"

    def feed(self, chunk: Any) -> bool:
        self.model = get(chunk, "model") or self.model
        if get(chunk, "usage") is not None:
            self.usage = _chat_usage(get(chunk, "usage"))
        first = False
        for choice in get(chunk, "choices", default=[]) or []:
            delta = get(choice, "delta")
            self.role = get(delta, "role") or self.role
            content = get(delta, "content")
            if content:
                self.text.append(content)
                first = True
            for call in get(delta, "tool_calls", default=[]) or []:
                slot = self.tool_calls.setdefault(
                    get(call, "index", default=0),
                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                )
                slot["id"] = get(call, "id") or slot["id"]
                slot["function"]["name"] += get(call, "function", "name", default="")
                slot["function"]["arguments"] += get(call, "function", "arguments", default="")
                first = True
        return first

    def output(self) -> Any:
        message: Dict[str, Any] = {"role": self.role, "content": "".join(self.text) or None}
        if self.tool_calls:
            message["tool_calls"] = [self.tool_calls[i] for i in sorted(self.tool_calls)]
        return message


class _ResponsesAccumulator(Accumulator):
    def __init__(self) -> None:
        super().__init__()
        self.text = []
        self.final: Any = None

    def feed(self, event: Any) -> bool:
        kind = get(event, "type", default="")
        if kind == "response.output_text.delta":
            self.text.append(get(event, "delta", default=""))
            return True
        if kind in ("response.completed", "response.incomplete", "response.failed"):
            response = get(event, "response")
            self.final = response
            self.model = get(response, "model") or self.model
            self.usage = _responses_usage(get(response, "usage"))
        return False

    def output(self) -> Any:
        if self.text:
            return "".join(self.text)
        return get(self.final, "output_text") or plain(get(self.final, "output"))


def wrap(client: Any, traceloom: Optional[Traceloom] = None) -> Any:
    """Trace calls made through `client`. Returns the same client."""

    def chat_begin(kwargs: Dict[str, Any]) -> Run:
        return Run.begin(
            traceloom,
            "openai-chat",
            kwargs.get("model"),
            plain(kwargs.get("messages")),
            request_params(kwargs, _CHAT_EXCLUDE),
        )

    def responses_begin(kwargs: Dict[str, Any]) -> Run:
        params = request_params(kwargs, _RESPONSES_EXCLUDE)
        if kwargs.get("instructions"):
            params["instructions"] = kwargs["instructions"]
        return Run.begin(
            traceloom, "openai-responses", kwargs.get("model"), plain(kwargs.get("input")), params
        )

    try:
        completions = client.chat.completions
        wrap_method(completions, "create", chat_begin, _chat_extract, _ChatAccumulator)
    except Exception:
        log.warning("Traceloom: could not instrument chat.completions", exc_info=True)
    try:
        responses = getattr(client, "responses", None)
        if responses is not None:
            wrap_method(
                responses, "create", responses_begin, _responses_extract, _ResponsesAccumulator
            )
    except Exception:
        log.warning("Traceloom: could not instrument responses", exc_info=True)
    return client
