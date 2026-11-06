"""Provider interface shared by every LLM adapter.

`stream(messages, model, params, tools, response_schema)` yields Deltas and never raises: a failure
becomes an ErrorDelta whose message has secrets scrubbed. Adapters implement `_stream`, which may
raise.

Canonical inputs (the same for every provider):
- messages: `{"role": "system"|"user"|"assistant"|"tool", "content": str}`. An assistant message may
  carry `tool_calls: [{"id", "name", "arguments": dict}]`; a tool message carries `tool_call_id`.
- params: `temperature`, `top_p`, `max_tokens`, `stop` (list of strings); other keys are ignored.
- tools: `[{"name", "description", "parameters": <JSON schema>}]`.
- response_schema: a JSON schema the reply must follow, or None.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# --- Deltas -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class UsageDelta:
    input_tokens: int = 0
    output_tokens: int = 0
    details: dict[str, int] = field(default_factory=dict)  # cache_read, cache_write, reasoning, ...

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class DoneDelta:
    finish_reason: str | None = None


@dataclass(frozen=True)
class ErrorDelta:
    message: str


Delta = TextDelta | ToolCallDelta | UsageDelta | DoneDelta | ErrorDelta


# --- Connection config and the Provider protocol --------------------------------------------------


@dataclass
class ProviderConfig:
    """A decrypted LLM connection as the adapters see it."""

    api_key: str = ""
    base_url: str | None = None
    config: dict[str, Any] = field(default_factory=dict)  # provider-specific, non-secret
    secrets: dict[str, str] = field(default_factory=dict)  # extra secret values to scrub
    timeout: float = 120.0
    allow_private: bool = True  # SSRF policy for base URLs (llm_allow_private)


@runtime_checkable
class Provider(Protocol):
    name: str

    def stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        params: Mapping[str, Any] | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        response_schema: Mapping[str, Any] | None = None,
    ) -> Iterator[Delta]: ...


# --- Secret scrubbing -----------------------------------------------------------------------------

_KEY_SHAPES = re.compile(
    r"sk-[A-Za-z0-9_\-]{8,}|AKIA[A-Z0-9]{12,}|ASIA[A-Z0-9]{12,}|AIza[0-9A-Za-z_\-]{20,}"
    r"|(?i:bearer)\s+[A-Za-z0-9._\-~+/=]{8,}"
)
REDACTED = "[redacted]"


def scrub(text: str, secrets: Sequence[str] = ()) -> str:
    """Remove the given secrets and anything shaped like an API key from `text`."""
    for secret in sorted((s for s in secrets if s and len(s) >= 4), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return _KEY_SHAPES.sub(REDACTED, text)


class BaseProvider:
    name = ""

    def __init__(self, config: ProviderConfig) -> None:
        self.cfg = config

    def secret_values(self) -> list[str]:
        return [self.cfg.api_key, *self.cfg.secrets.values()]

    def scrub(self, text: str) -> str:
        return scrub(text, self.secret_values())

    def stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        params: Mapping[str, Any] | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        response_schema: Mapping[str, Any] | None = None,
    ) -> Iterator[Delta]:
        try:
            yield from self._stream(messages, model, params or {}, tools or (), response_schema)
        except Exception as exc:
            yield ErrorDelta(self.scrub(f"{type(exc).__name__}: {exc}"))

    def _stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        params: Mapping[str, Any],
        tools: Sequence[Mapping[str, Any]],
        response_schema: Mapping[str, Any] | None,
    ) -> Iterator[Delta]:
        raise NotImplementedError


def split_system(messages: Sequence[Mapping[str, Any]]) -> tuple[str, list[Mapping[str, Any]]]:
    """System text (joined) and the remaining messages in order."""
    system = [str(m.get("content") or "") for m in messages if m.get("role") == "system"]
    return "\n\n".join(s for s in system if s), [m for m in messages if m.get("role") != "system"]


# --- Registry -------------------------------------------------------------------------------------

Factory = Callable[[ProviderConfig], Provider]

PROVIDERS = ("anthropic", "openai", "azure_openai", "bedrock", "google", "openai_compatible")
# Real adapters are imported on first use so a missing SDK only matters for its own provider.
_BUILTIN = {
    "anthropic": "traceloom.llm.providers.anthropic:AnthropicProvider",
    "openai": "traceloom.llm.providers.openai:OpenAIProvider",
    "azure_openai": "traceloom.llm.providers.azure_openai:AzureOpenAIProvider",
    "bedrock": "traceloom.llm.providers.bedrock:BedrockProvider",
    "google": "traceloom.llm.providers.google:GoogleProvider",
    "openai_compatible": "traceloom.llm.providers.openai_compatible:OpenAICompatibleProvider",
}
_registered: dict[str, Factory] = {}


class UnknownProvider(KeyError):
    pass


def register(name: str, factory: Factory) -> None:
    """Add or replace an adapter (tests register a fake this way)."""
    _registered[name] = factory


def unregister(name: str) -> None:
    _registered.pop(name, None)


def get_factory(name: str) -> Factory:
    if name in _registered:
        return _registered[name]
    target = _BUILTIN.get(name)
    if target is None:
        raise UnknownProvider(name)
    module, _, attr = target.partition(":")
    return getattr(importlib.import_module(module), attr)


def create(name: str, config: ProviderConfig) -> Provider:
    return get_factory(name)(config)
