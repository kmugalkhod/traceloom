"""Google Gemini adapter over the google-genai SDK (imported on first use).

Params understood: `max_tokens`, `temperature`, `top_p`, `stop`. A custom `base_url` points the
SDK at a proxy or gateway.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from traceloom.llm.providers.base import (
    BaseProvider,
    Delta,
    DoneDelta,
    ProviderConfig,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
    split_system,
)


def map_contents(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    names: dict[str, str] = {}  # tool call id -> function name, for function responses

    def push(role: str, part: dict[str, Any]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["parts"].append(part)
        else:
            out.append({"role": role, "parts": [part]})

    for m in messages:
        role, text = m.get("role"), str(m.get("content") or "")
        if role == "tool":
            name = names.get(m["tool_call_id"], m["tool_call_id"])
            push("user", {"function_response": {"name": name, "response": {"result": text}}})
        elif role == "assistant":
            if text:
                push("model", {"text": text})
            for call in m.get("tool_calls") or ():
                names[call["id"]] = call["name"]
                fn = {"name": call["name"], "args": call.get("arguments") or {}}
                push("model", {"function_call": fn})
        else:
            push("user", {"text": text})
    return out


class GoogleProvider(BaseProvider):
    name = "google"

    def __init__(self, config: ProviderConfig, *, http_client: Any = None) -> None:
        super().__init__(config)
        self._http_client = http_client

    def _client(self) -> Any:
        from google import genai

        options: dict[str, Any] = {"timeout": int(self.cfg.timeout * 1000)}  # milliseconds
        if self.cfg.base_url:
            options["base_url"] = self.cfg.base_url
        if self._http_client is not None:
            options["httpx_client"] = self._http_client
        return genai.Client(api_key=self.cfg.api_key, http_options=options)

    def build_request(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        params: Mapping[str, Any],
        tools: Sequence[Mapping[str, Any]],
        response_schema: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        system, rest = split_system(messages)
        config: dict[str, Any] = {}
        if system:
            config["system_instruction"] = system
        for source, target in (
            ("max_tokens", "max_output_tokens"),
            ("temperature", "temperature"),
            ("top_p", "top_p"),
        ):
            if params.get(source) is not None:
                config[target] = params[source]
        if params.get("stop"):
            config["stop_sequences"] = list(params["stop"])
        if tools:
            from google.genai import types

            declarations = [
                types.FunctionDeclaration(
                    name=t["name"],
                    description=t.get("description", ""),
                    parameters_json_schema=t.get("parameters")
                    or {"type": "object", "properties": {}},
                )
                for t in tools
            ]
            config["tools"] = [types.Tool(function_declarations=declarations)]
            # we run the tool loop ourselves; the SDK must not call anything
            config["automatic_function_calling"] = {"disable": True}
        if response_schema:
            config["response_mime_type"] = "application/json"
            config["response_json_schema"] = dict(response_schema)
        return {"model": model, "contents": map_contents(rest), "config": config}

    def _stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        params: Mapping[str, Any],
        tools: Sequence[Mapping[str, Any]],
        response_schema: Mapping[str, Any] | None,
    ) -> Iterator[Delta]:
        request = self.build_request(messages, model, params, tools, response_schema)
        finish: str | None = None
        usage: Any = None
        calls = 0
        for chunk in self._client().models.generate_content_stream(**request):
            if chunk.usage_metadata is not None:
                usage = chunk.usage_metadata
            for candidate in chunk.candidates or ():
                if candidate.finish_reason is not None:
                    finish = getattr(candidate.finish_reason, "value", str(candidate.finish_reason))
                for part in (candidate.content.parts if candidate.content else None) or ():
                    if part.function_call is not None:
                        call = part.function_call
                        calls += 1
                        yield ToolCallDelta(
                            call.id or f"call_{calls}", call.name, dict(call.args or {})
                        )
                    elif part.text and not part.thought:
                        yield TextDelta(part.text)
        if usage is not None:
            yield _usage(usage)
        yield DoneDelta(finish)


def _usage(u: Any) -> UsageDelta:
    details = {}
    if u.cached_content_token_count:
        details["cache_read"] = int(u.cached_content_token_count)
    if u.thoughts_token_count:
        details["reasoning"] = int(u.thoughts_token_count)
    output = int(u.candidates_token_count or 0) + int(u.thoughts_token_count or 0)
    return UsageDelta(int(u.prompt_token_count or 0), output, details)


__all__ = ["GoogleProvider", "map_contents"]
