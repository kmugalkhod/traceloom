"""OpenAI adapter (Chat Completions streaming) over the official SDK, imported on first use.

Azure OpenAI and OpenAI-compatible servers reuse everything here and only differ in how the SDK
client is built (see `_make_client`) and in a few request details (`_token_param`, `_deployment`).

Params understood: `max_tokens`, `temperature`, `top_p`, `stop`, `effort` (reasoning effort).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from traceloom.llm.providers.base import (
    BaseProvider,
    Delta,
    DoneDelta,
    ErrorDelta,
    ProviderConfig,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
)


def map_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m["tool_call_id"],
                    "content": str(m.get("content") or ""),
                }
            )
        elif role == "assistant" and m.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": m.get("content") or None,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": json.dumps(c.get("arguments") or {}),
                            },
                        }
                        for c in m["tool_calls"]
                    ],
                }
            )
        else:
            out.append({"role": role, "content": str(m.get("content") or "")})
    return out


def _parse_args(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_value": value}


class OpenAIProvider(BaseProvider):
    name = "openai"
    _token_param = "max_completion_tokens"

    def __init__(self, config: ProviderConfig, *, http_client: Any = None) -> None:
        super().__init__(config)
        self._http_client = http_client

    # --- client construction (the part that differs per flavour) ------------------------------

    def _client_kwargs(self) -> dict[str, Any]:
        """SDK client arguments. Without an injected client, a fresh SSRF-pinned one is built
        per call, and closing the SDK client closes it."""
        from traceloom.llm.http import httpx_module

        kwargs: dict[str, Any] = {
            "api_key": self.cfg.api_key,
            "timeout": self.cfg.timeout,
            "max_retries": 0,
        }
        kwargs["http_client"] = self._http_client or self.pinned_http_client(httpx_module("openai"))
        return kwargs

    def _make_client(self) -> Any:
        import openai

        kwargs = self._client_kwargs()
        if self.cfg.base_url:
            kwargs["base_url"] = self.cfg.base_url
        return openai.OpenAI(**kwargs)

    def _deployment(self, model: str) -> str:
        return model

    # --- request ----------------------------------------------------------------------------

    def build_request(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        params: Mapping[str, Any],
        tools: Sequence[Mapping[str, Any]],
        response_schema: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self._deployment(model),
            "messages": map_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if params.get("max_tokens"):
            request[self._token_param] = int(params["max_tokens"])
        for key in ("temperature", "top_p"):
            if params.get(key) is not None:
                request[key] = params[key]
        if params.get("stop"):
            request["stop"] = list(params["stop"])
        if params.get("effort"):
            request["reasoning_effort"] = params["effort"]
        if tools:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                    },
                }
                for t in tools
            ]
        if response_schema:
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": dict(response_schema)},
            }
        return request

    # --- stream -----------------------------------------------------------------------------

    def _stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        params: Mapping[str, Any],
        tools: Sequence[Mapping[str, Any]],
        response_schema: Mapping[str, Any] | None,
    ) -> Iterator[Delta]:
        request = self.build_request(messages, model, params, tools, response_schema)
        calls: dict[int, dict[str, str]] = {}
        finish: str | None = None
        usage: UsageDelta | None = None
        client: Any = None
        stream: Any = None
        try:
            client = self._make_client()
            stream = client.chat.completions.create(**request)
            for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = _usage(chunk.usage)
                for choice in chunk.choices or ():
                    delta = choice.delta
                    if delta is not None:
                        if getattr(delta, "refusal", None):
                            yield ErrorDelta(f"The model declined to answer: {delta.refusal}")
                        if delta.content:
                            yield TextDelta(delta.content)
                        for part in delta.tool_calls or ():
                            call = calls.setdefault(part.index, {"id": "", "name": "", "args": ""})
                            call["id"] = part.id or call["id"]
                            if part.function is not None:
                                call["name"] = part.function.name or call["name"]
                                call["args"] += part.function.arguments or ""
                    if choice.finish_reason:
                        finish = choice.finish_reason
                        for index in sorted(calls):
                            c = calls[index]
                            yield ToolCallDelta(c["id"], c["name"], _parse_args(c["args"]))
                        calls.clear()
        finally:
            # Closing the SDK stream drops the HTTP connection, which is how the provider learns
            # that nobody is listening any more (a cancelled run, a closed playground tab).
            if stream is not None:
                stream.close()
            if client is not None and self._http_client is None:
                client.close()
        if usage is not None:
            yield usage
        yield DoneDelta(finish)


def _usage(u: Any) -> UsageDelta:
    """`prompt_tokens` already counts the cached tokens; input is the uncached rest, so cost
    charges each token once (input rate + cache_read rate)."""
    details: dict[str, int] = {}
    cached = int(getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", None) or 0)
    reasoning = getattr(getattr(u, "completion_tokens_details", None), "reasoning_tokens", None)
    if cached:
        details["cache_read"] = cached
    if reasoning:
        details["reasoning"] = int(reasoning)
    prompt = int(u.prompt_tokens or 0)
    return UsageDelta(max(prompt - cached, 0), int(u.completion_tokens or 0), details)
