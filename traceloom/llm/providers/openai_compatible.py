"""OpenAI-compatible servers (LiteLLM, vLLM, Ollama, gateways): a required base URL and headers.

The base URL goes through the SSRF guard when the connection is built, again before each stream,
and on every request (the pinned HTTP client): metadata and link-local addresses are always
refused, private ones unless the project's policy (`allow_private`, default on for LLM
connections) allows them. `config["headers"]` adds
custom request headers.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from traceloom.llm.providers.base import Delta, ProviderConfig
from traceloom.llm.providers.openai import OpenAIProvider
from traceloom.llm.ssrf import Resolver, resolve_target, system_resolver


class OpenAICompatibleProvider(OpenAIProvider):
    name = "openai_compatible"
    _token_param = "max_tokens"  # what most compatible servers understand

    def __init__(
        self,
        config: ProviderConfig,
        *,
        http_client: Any = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        if not config.base_url:
            raise ValueError("an OpenAI-compatible connection needs a base URL")
        super().__init__(config, http_client=http_client)
        if resolver is not system_resolver:
            self.ssrf_resolver = resolver
        self._check_url()  # raises BlockedAddress for a refused target

    def _check_url(self) -> None:
        resolve_target(
            self.cfg.base_url or "",
            allow_private=self.cfg.allow_private,
            resolver=self.ssrf_resolver,
        )

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs = super()._client_kwargs()
        kwargs["api_key"] = self.cfg.api_key or "none"  # keyless local servers
        if headers := self.cfg.config.get("headers"):
            kwargs["default_headers"] = dict(headers)
        return kwargs

    def secret_values(self) -> list[str]:
        headers = self.cfg.config.get("headers") or {}
        return [*super().secret_values(), *map(str, headers.values())]

    def _stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        params: Mapping[str, Any],
        tools: Sequence[Mapping[str, Any]],
        response_schema: Mapping[str, Any] | None,
    ) -> Iterator[Delta]:
        self._check_url()  # DNS may have changed since the connection was saved
        yield from super()._stream(messages, model, params, tools, response_schema)
