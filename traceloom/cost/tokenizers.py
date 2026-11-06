"""Token counting for generations that report no usage. tiktoken is optional and lazy.

Encodings are downloaded on first use; offline (or without tiktoken) the count is simply
unavailable and the generation keeps empty usage.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger("traceloom.cost")

_lock = threading.Lock()
_encoders: dict[str, Any] = {}  # name -> encoder, or None when it cannot be loaded


def get_encoder(tokenizer: str | None) -> Any | None:
    if not tokenizer or not tokenizer.startswith("tiktoken:"):
        return None
    name = tokenizer.split(":", 1)[1]
    with _lock:
        if name not in _encoders:
            try:
                import tiktoken

                _encoders[name] = tiktoken.get_encoding(name)
            except Exception as exc:  # missing package, no network, unknown encoding
                log.warning("tokenizer %s unavailable: %s", tokenizer, exc)
                _encoders[name] = None
        return _encoders[name]


def count_tokens(tokenizer: str | None, text: str) -> int | None:
    """Tokens in `text`, or None when the tokenizer is unavailable."""
    encoder = get_encoder(tokenizer)
    if encoder is None:
        return None
    return len(encoder.encode(text, disallowed_special=()))
