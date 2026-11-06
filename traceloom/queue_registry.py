"""Queue message types and the registry that routes a message to its handler.

A message without a `type` is an ingest batch. Both queue adapters own one registry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

INGEST = "ingest"
WEBHOOK_DELIVERY = "webhook_delivery"

Handler = Callable[[Mapping[str, Any]], None]


class UnknownMessageType(ValueError):
    pass


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, message_type: str, handler: Handler) -> None:
        self._handlers[message_type] = handler

    def clear(self) -> None:
        self._handlers.clear()

    def has(self, message_type: str) -> bool:
        return message_type in self._handlers

    def dispatch(self, message: Mapping[str, Any]) -> None:
        message_type = message.get("type") or INGEST
        handler = self._handlers.get(message_type)
        if handler is None:
            raise UnknownMessageType(f"no handler registered for message type {message_type!r}")
        handler(message)
