"""Amazon Bedrock adapter over boto3's bedrock-runtime `converse_stream` (imported on first use).

Credentials: `api_key` is the secret access key; `secrets["access_key_id"]` the access key id and
optionally `secrets["session_token"]`; `config["region"]` the region. All of them are scrubbed
from errors. Params understood: `max_tokens`, `temperature`, `top_p`, `stop`.
"""

from __future__ import annotations

import json
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


def map_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def push(role: str, block: dict[str, Any]) -> None:
        if out and out[-1]["role"] == role:  # Converse wants strictly alternating roles
            out[-1]["content"].append(block)
        else:
            out.append({"role": role, "content": [block]})

    for m in messages:
        role = m.get("role")
        text = str(m.get("content") or "")
        if role == "tool":
            result = {"toolUseId": m["tool_call_id"], "content": [{"text": text}]}
            push("user", {"toolResult": result})
        elif role == "assistant":
            if text:
                push("assistant", {"text": text})
            for call in m.get("tool_calls") or ():
                use = {
                    "toolUseId": call["id"],
                    "name": call["name"],
                    "input": call.get("arguments") or {},
                }
                push("assistant", {"toolUse": use})
        else:
            push("user", {"text": text})
    return out


class BedrockProvider(BaseProvider):
    name = "bedrock"

    def __init__(self, config: ProviderConfig, *, client: Any = None) -> None:
        super().__init__(config)
        self._client_override = client

    def _client(self) -> Any:
        if self._client_override is not None:
            return self._client_override
        import boto3
        from botocore.config import Config

        return boto3.client(
            "bedrock-runtime",
            region_name=self.cfg.config.get("region") or "us-east-1",
            aws_access_key_id=self.cfg.secrets.get("access_key_id") or None,
            aws_secret_access_key=self.cfg.api_key or None,
            aws_session_token=self.cfg.secrets.get("session_token") or None,
            endpoint_url=self.cfg.base_url or None,
            config=Config(
                read_timeout=self.cfg.timeout, retries={"max_attempts": 1, "mode": "standard"}
            ),
        )

    def build_request(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        params: Mapping[str, Any],
        tools: Sequence[Mapping[str, Any]],
        response_schema: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        system, rest = split_system(messages)
        request: dict[str, Any] = {"modelId": model, "messages": map_messages(rest)}
        if system:
            request["system"] = [{"text": system}]
        inference: dict[str, Any] = {}
        if params.get("max_tokens"):
            inference["maxTokens"] = int(params["max_tokens"])
        if params.get("temperature") is not None:
            inference["temperature"] = params["temperature"]
        if params.get("top_p") is not None:
            inference["topP"] = params["top_p"]
        if params.get("stop"):
            inference["stopSequences"] = list(params["stop"])
        if inference:
            request["inferenceConfig"] = inference
        if tools:
            request["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "inputSchema": {
                                "json": t.get("parameters") or {"type": "object", "properties": {}}
                            },
                        }
                    }
                    for t in tools
                ]
            }
        if response_schema:
            request["outputConfig"] = {
                "textFormat": {
                    "type": "json_schema",
                    "structure": {
                        "jsonSchema": {"name": "response", "schema": json.dumps(response_schema)}
                    },
                }
            }
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
        response = self._client().converse_stream(**request)
        pending: dict[int, dict[str, str]] = {}
        stop_reason: str | None = None
        usage: UsageDelta | None = None
        stream = response["stream"]
        try:
            for event in stream:
                if "contentBlockStart" in event:
                    start = event["contentBlockStart"]
                    use = (start.get("start") or {}).get("toolUse")
                    if use:
                        pending[start["contentBlockIndex"]] = {
                            "id": use["toolUseId"],
                            "name": use["name"],
                            "json": "",
                        }
                elif "contentBlockDelta" in event:
                    block = event["contentBlockDelta"]
                    delta = block.get("delta") or {}
                    if delta.get("text"):
                        yield TextDelta(delta["text"])
                    elif "toolUse" in delta and block["contentBlockIndex"] in pending:
                        pending[block["contentBlockIndex"]]["json"] += delta["toolUse"].get(
                            "input", ""
                        )
                elif "contentBlockStop" in event:
                    call = pending.pop(event["contentBlockStop"]["contentBlockIndex"], None)
                    if call:
                        yield ToolCallDelta(call["id"], call["name"], _parse_args(call["json"]))
                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason")
                elif "metadata" in event:
                    usage = _usage(event["metadata"].get("usage") or {})
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        if usage is not None:
            yield usage
        yield DoneDelta(stop_reason)


def _usage(u: Mapping[str, Any]) -> UsageDelta:
    details = {}
    if u.get("cacheReadInputTokens"):
        details["cache_read"] = int(u["cacheReadInputTokens"])
    if u.get("cacheWriteInputTokens"):
        details["cache_write"] = int(u["cacheWriteInputTokens"])
    return UsageDelta(int(u.get("inputTokens", 0)), int(u.get("outputTokens", 0)), details)


def _parse_args(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_value": value}
