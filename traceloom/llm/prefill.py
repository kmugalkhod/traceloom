"""Turn a traced generation into playground inputs (FR-L5).

Generations recorded by the SDK wrappers keep the request's messages as `input` and every other
request parameter in `model_parameters`, in the producing provider's shape (OpenAI chat or Anthropic
messages). This normalises both into the playground's canonical shape (see llm/providers/base.py).
"""

from __future__ import annotations

import json
from typing import Any

PARAM_KEYS = ("temperature", "top_p", "max_tokens")


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return "" if content is None else json.dumps(content, default=str)


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {"_raw": raw}
    return raw if isinstance(raw, dict) else {}


def _message(m: dict[str, Any]) -> list[dict[str, Any]]:
    role = "system" if m.get("role") == "developer" else m.get("role")
    content = m.get("content")
    if role == "tool":
        return [
            {"role": "tool", "tool_call_id": m.get("tool_call_id", ""), "content": _text(content)}
        ]
    blocks = content if isinstance(content, list) else []
    results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
    out: list[dict[str, Any]] = [
        {
            "role": "tool",
            "tool_call_id": b.get("tool_use_id", ""),
            "content": _text(b.get("content")),
        }
        for b in results
    ]
    calls = [
        {"id": b.get("id", ""), "name": b.get("name", ""), "arguments": _arguments(b.get("input"))}
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    for c in m.get("tool_calls") or ():  # OpenAI shape
        fn = c.get("function") or {}
        calls.append(
            {
                "id": c.get("id", ""),
                "name": fn.get("name", ""),
                "arguments": _arguments(fn.get("arguments")),
            }
        )
    text = _text(content)
    if text or calls or not results:
        message: dict[str, Any] = {"role": role or "user", "content": text}
        if calls:
            message["tool_calls"] = calls
        out.append(message)
    return out


def messages_from(input_: Any, system: Any = None) -> list[dict[str, Any]]:
    if isinstance(input_, str):
        input_ = [{"role": "user", "content": input_}]
    if isinstance(input_, dict) and isinstance(input_.get("messages"), list):
        input_ = input_["messages"]
    if not isinstance(input_, list):
        input_ = [{"role": "user", "content": json.dumps(input_, default=str)}]
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": _text(system)})
    for m in input_:
        out.extend(_message(m) if isinstance(m, dict) else [{"role": "user", "content": str(m)}])
    return out


def tools_from(raw: Any) -> list[dict[str, Any]]:
    tools = []
    for t in raw if isinstance(raw, list) else ():
        if not isinstance(t, dict):
            continue
        spec = t.get("function") if isinstance(t.get("function"), dict) else t
        if spec.get("name"):
            tools.append(
                {
                    "name": spec["name"],
                    "description": spec.get("description", ""),
                    "parameters": spec.get("parameters") or spec.get("input_schema") or {},
                }
            )
    return tools


def params_from(model_parameters: dict[str, Any]) -> dict[str, Any]:
    p = model_parameters or {}
    out = {k: p[k] for k in PARAM_KEYS if p.get(k) is not None}
    if p.get("max_completion_tokens") is not None and "max_tokens" not in out:
        out["max_tokens"] = p["max_completion_tokens"]
    stop = p.get("stop") or p.get("stop_sequences")
    if stop:
        out["stop"] = [stop] if isinstance(stop, str) else list(stop)
    return out


def guess_provider(model: str | None) -> str | None:
    m = (model or "").lower()
    if m.startswith(("anthropic.", "amazon.", "meta.", "mistral.", "us.", "eu.")):
        return "bedrock"
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    if m.startswith("gemini"):
        return "google"
    return None


def from_observation(obs: Any) -> dict[str, Any]:
    """Playground fields for an ObservationRow."""
    mp = obs.model_parameters or {}
    return {
        "model": obs.model,
        "provider": guess_provider(obs.model),
        "messages": messages_from(obs.input, mp.get("system")),
        "params": params_from(mp),
        "tools": tools_from(mp.get("tools")),
        "prompt_name": obs.prompt_name,
        "prompt_version": obs.prompt_version,
        "trace_id": obs.trace_id,
        "observation_id": obs.id,
    }
