"""Outgoing HTTP for the LLM provider SDKs, with the SSRF guard on every request.

`pinned_client` builds the `http_client` / `httpx_client` handed to the openai, azure_openai,
openai_compatible, anthropic and google adapters. Its transport runs `resolve_target` for each
request it sends (redirect hops and retries included, though redirects are off), then connects
to the address that check approved, keeping the original Host header and TLS server name, as
webhook delivery does. A second DNS answer therefore cannot swap in another address between the
check and the connect, and metadata and link-local addresses stay blocked even when private
addresses are allowed.

Recent SDKs ship their own httpx generation (`httpx2`); `httpx_module` picks the one an SDK
accepts, and the transport is built from that same module.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

from traceloom.llm.ssrf import BlockedAddress, Resolver, resolve_target, system_resolver

__all__ = ["BlockedAddress", "httpx_module", "pinned_client", "pinned_transport"]


def httpx_module(sdk: str | None = None) -> ModuleType:
    """The httpx generation `sdk` ("openai", "anthropic") builds its clients with; for None (any
    SDK that takes both, like google-genai) httpx2 when installed, else httpx."""
    if sdk is not None:
        base = importlib.import_module(f"{sdk}._base_client")
        module = getattr(base, "httpx2", None) or getattr(base, "httpx", None)
        if module is not None:
            return module
    try:
        return importlib.import_module("httpx2")
    except ImportError:
        return importlib.import_module("httpx")


def pinned_transport(
    module: ModuleType,
    *,
    allow_private: bool,
    resolver: Resolver = system_resolver,
    inner: Any = None,
) -> Any:
    """A `module.BaseTransport` that checks and pins every request before `inner` sends it."""

    class PinnedTransport(module.BaseTransport):  # type: ignore[name-defined, misc]
        def __init__(self) -> None:
            self._inner = inner if inner is not None else module.HTTPTransport()

        def handle_request(self, request: Any) -> Any:
            target = resolve_target(
                str(request.url), allow_private=allow_private, resolver=resolver
            )
            request.url = module.URL(target.connect_url)
            request.headers["Host"] = target.host_header
            request.extensions = {**request.extensions, "sni_hostname": target.host}
            return self._inner.handle_request(request)

        def close(self) -> None:
            self._inner.close()

    return PinnedTransport()


def pinned_client(
    module: ModuleType,
    *,
    allow_private: bool,
    timeout: float,
    resolver: Resolver = system_resolver,
    inner: Any = None,
) -> Any:
    """A `module.Client` whose every request is SSRF-checked and pinned, never following
    redirects and ignoring proxy environment variables (a proxy would connect elsewhere)."""
    return module.Client(
        transport=pinned_transport(
            module, allow_private=allow_private, resolver=resolver, inner=inner
        ),
        follow_redirects=False,
        trust_env=False,
        timeout=timeout,
    )
