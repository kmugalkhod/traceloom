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


ALL_PARAMS = ["temperature", "top_p", "max_tokens", "stop"]

# The one rule set for which generation parameters a model takes. The playground page receives
# these rules as data (patterns are plain `re` / JavaScript compatible), so the server and the page
# can never disagree. First matching rule wins; no match means every parameter.
PARAM_RULES: list[dict] = [
    {  # Claude 5 family (any id: bare, dated, -latest, prefixed): adaptive thinking, no sampling
        "providers": ["anthropic"],
        "pattern": r"claude-(?:fable|opus|sonnet|haiku)-5(?:$|[-.:_@])",
        "params": ["max_tokens"],
    },
    {  # OpenAI reasoning models fix temperature and top_p
        "providers": ["openai", "azure_openai"],
        "pattern": r"(?:^|[/.])(?:gpt-5|o\d)(?:$|[-.:_@])",
        "params": ["max_tokens"],
    },
]
_COMPILED = [(r, re.compile(r["pattern"])) for r in PARAM_RULES]


def supported_params(provider: str, model: str) -> list[str]:
    """The generation parameters the playground offers for this model."""
    for rule, pattern in _COMPILED:
        if provider in rule["providers"] and pattern.search(model or ""):
            return list(rule["params"])
    return list(ALL_PARAMS)
