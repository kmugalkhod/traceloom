"""Built-in model lists per provider; a connection adds its own custom models on top."""

from __future__ import annotations

import re
from collections.abc import Iterable

_OPENAI = ["gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini"]

DEFAULT_MODELS: dict[str, list[str]] = {
    "anthropic": [
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-4-5-20251001",
    ],
    "openai": _OPENAI,
    "azure_openai": _OPENAI,  # model names; the connection maps each to a deployment
    "bedrock": [
        "anthropic.claude-sonnet-4-5-20250929-v1:0",
        "anthropic.claude-haiku-4-5-20251001-v1:0",
        "amazon.nova-pro-v1:0",
        "amazon.nova-lite-v1:0",
    ],
    "google": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
    "openai_compatible": ["llama3.3", "qwen3", "mistral"],  # typical local / gateway names
}


def models_for(provider: str, custom: Iterable[str] = ()) -> list[str]:
    """Default models followed by the connection's custom ones, without duplicates."""
    return list(dict.fromkeys([*DEFAULT_MODELS.get(provider, []), *custom]))


_ALL_PARAMS = ["temperature", "top_p", "max_tokens", "stop"]
_CLAUDE_5 = re.compile(r"claude-(?:fable|opus|sonnet|haiku)-5")
_OPENAI_REASONING = re.compile(r"^(?:gpt-5|o\d)")


def supported_params(provider: str, model: str) -> list[str]:
    """The generation parameters the playground offers for this model.

    Claude 5-family models take no sampling parameters (adaptive thinking only), and OpenAI
    reasoning models fix temperature and top_p. Everything else takes the usual four.
    """
    if provider == "anthropic" and _CLAUDE_5.search(model):
        return ["max_tokens"]
    if provider in ("openai", "azure_openai") and _OPENAI_REASONING.match(model):
        return ["max_tokens"]
    return list(_ALL_PARAMS)
