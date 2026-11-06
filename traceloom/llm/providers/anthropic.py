"""Anthropic adapter over the official SDK (imported on first use).

Follows the current Messages API shapes: adaptive thinking only (`budget_tokens` is sent solely
for models that still take it, never for the 5 family), no assistant prefill, no sampling
parameters on 5-family models, and `stop_reason == "refusal"` shown inline.

Params understood: `max_tokens`, `temperature`, `top_p`, `stop`, `thinking` (true: adaptive),
`thinking_budget` (older models only) and `effort` (low..max).
"""

from __future__ import annotations

import json
import re
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
    split_system,
)

DEFAULT_MAX_TOKENS = 16000
# Claude 5 family (fable/mythos/opus/sonnet 5, 5.1, 5.5): no sampling params, no budget_tokens.
_FIVE_FAMILY = re.compile(r"^claude-(fable|mythos|opus|sonnet)-5(\b|-)")
# 4.6+ models take adaptive thinking too; they still accept sampling params.
_ADAPTIVE = re.compile(r"^claude-(opus|sonnet)-4-([6-9]|\d{2})")


def is_five_family(model: str) -> bool:
    return bool(_FIVE_FAMILY.match(model))


def _content_blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if message.get("content"):
        blocks.append({"type": "text", "text": str(message["content"])})
    for call in message.get("tool_calls") or ():
        blocks.append(
            {
                "type": "tool_use",
                "id": call["id"],
                "name": call["name"],
                "input": call.get("arguments") or {},
            }
        )
    return blocks


def map_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Canonical messages (no system) to Anthropic messages; tool results become user blocks."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m["tool_call_id"],
                "content": str(m.get("content") or ""),
            }
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)  # results of parallel calls share one message
            else:
                out.append({"role": "user", "content": [block]})
        elif role == "assistant":
            blocks = _content_blocks(m)
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": "user", "content": str(m.get("content") or "")})
    return out


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def __init__(self, config: ProviderConfig, *, http_client: Any = None) -> None:
        super().__init__(config)
        self._http_client = http_client

    def _client(self) -> Any:
        import anthropic

        kwargs: dict[str, Any] = {
            "api_key": self.cfg.api_key,
            "timeout": self.cfg.timeout,
            "max_retries": 0,  # the playground reports failures instead of waiting
        }
        if self.cfg.base_url:
            kwargs["base_url"] = self.cfg.base_url
        if self._http_client is not None:
            kwargs["http_client"] = self._http_client
        return anthropic.Anthropic(**kwargs)

    def build_request(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        params: Mapping[str, Any],
        tools: Sequence[Mapping[str, Any]],
        response_schema: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        system, rest = split_system(messages)
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": int(params.get("max_tokens") or DEFAULT_MAX_TOKENS),
            "messages": map_messages(rest),
        }
        if system:
            request["system"] = system
        five = is_five_family(model)
        if not five:  # sampling parameters are rejected on the 5 family
            for key in ("temperature", "top_p"):
                if params.get(key) is not None:
                    request[key] = params[key]
        if params.get("stop"):
            request["stop_sequences"] = list(params["stop"])
        if params.get("thinking") and (five or _ADAPTIVE.match(model)):
            request["thinking"] = {"type": "adaptive"}
        elif params.get("thinking_budget") and not five and not _ADAPTIVE.match(model):
            request["thinking"] = {
                "type": "enabled",
                "budget_tokens": int(params["thinking_budget"]),
            }
        output_config: dict[str, Any] = {}
        if params.get("effort"):
            output_config["effort"] = params["effort"]
        if response_schema:
            output_config["format"] = {"type": "json_schema", "schema": dict(response_schema)}
        if output_config:
            request["output_config"] = output_config
        if tools:
            request["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters") or {"type": "object", "properties": {}},
                }
                for t in tools
            ]
        return request

    def _stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        params: Mapping[str, Any],
        tools: Sequence[Mapping[str, Any]],
        response_schema: Mapping[str, Any] | None,
    ) -> Iterator[Delta]:
        request = self.build_request(messages, model, params, tools, response_schema)
        usage: dict[str, int] = {}
        stop_reason: str | None = None
        stop_category: str | None = None
        pending: dict[int, dict[str, Any]] = {}  # block index -> {id, name, json}
        # Recent SDKs no longer type the sampling parameters; older models still accept them.
        extra = {k: request.pop(k) for k in ("temperature", "top_p") if k in request}
        with self._client().messages.stream(**request, extra_body=extra or None) as stream:
            for event in stream:
                kind = event.type
                if kind == "message_start":
                    usage.update(_usage_fields(event.message.usage))
                elif kind == "content_block_start" and event.content_block.type == "tool_use":
                    block = event.content_block
                    pending[event.index] = {"id": block.id, "name": block.name, "json": ""}
                elif kind == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield TextDelta(delta.text)
                    elif delta.type == "input_json_delta" and event.index in pending:
                        pending[event.index]["json"] += delta.partial_json
                elif kind == "content_block_stop" and event.index in pending:
                    call = pending.pop(event.index)
                    yield ToolCallDelta(call["id"], call["name"], _parse_args(call["json"]))
                elif kind == "message_delta":
                    stop_reason = event.delta.stop_reason or stop_reason
                    details = getattr(event, "stop_details", None) or getattr(
                        event.delta, "stop_details", None
                    )
                    stop_category = getattr(details, "category", None) or stop_category
                    usage.update(_usage_fields(event.usage))
        yield UsageDelta(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            details={
                k: v for k, v in usage.items() if k not in ("input_tokens", "output_tokens") and v
            },
        )
        if stop_reason == "refusal":
            suffix = f" ({stop_category})" if stop_category else ""
            yield ErrorDelta(f"The model declined to answer this request{suffix}.")
        yield DoneDelta(stop_reason)


def _usage_fields(usage: Any) -> dict[str, int]:
    names = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "cache_read_input_tokens": "cache_read",
        "cache_creation_input_tokens": "cache_write",
    }
    out: dict[str, int] = {}
    for source, target in names.items():
        value = getattr(usage, source, None)
        if value is not None:
            out[target] = int(value)
    return out


def _parse_args(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_value": value}
