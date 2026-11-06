"""Project API keys: `tl-pk-...` / `tl-sk-...` pairs, HTTP Basic, hashed secrets (ADR-0009)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

PUBLIC_PREFIX = "tl-pk-"
SECRET_PREFIX = "tl-sk-"


def generate_key_pair() -> tuple[str, str]:
    """(public_key, secret_key). The secret is shown once; only its hash is stored."""
    return PUBLIC_PREFIX + secrets.token_urlsafe(18), SECRET_PREFIX + secrets.token_urlsafe(32)


def hash_secret(secret: str, server_key: str) -> str:
    """HMAC-SHA256(secret, TRACELOOM_SERVER_SECRET): useless without the server secret."""
    return hmac.new(server_key.encode(), secret.encode(), hashlib.sha256).hexdigest()


def verify_secret(secret: str, stored_hash: str, server_key: str) -> bool:
    return hmac.compare_digest(hash_secret(secret, server_key), stored_hash)


@dataclass(frozen=True)
class ResolvedKey:
    key_id: str
    project_id: str
    public_key: str
    may_move_protected_labels: bool = False


class ApiKeyCache:
    """TTL cache of resolved keys, keyed by (public key, secret hash).

    Only successes are cached. `evict(public_key)` drops every entry for that key at once, so
    a revoke takes effect immediately in this process; the TTL bounds staleness elsewhere.
    """

    def __init__(self, ttl_seconds: float = 10.0, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], tuple[float, ResolvedKey]] = {}

    def get(self, public_key: str, secret_hash: str) -> ResolvedKey | None:
        with self._lock:
            entry = self._entries.get((public_key, secret_hash))
            if entry is None:
                return None
            expires, value = entry
            if expires <= self._clock():
                del self._entries[(public_key, secret_hash)]
                return None
            return value

    def put(self, public_key: str, secret_hash: str, value: ResolvedKey) -> None:
        with self._lock:
            self._entries[(public_key, secret_hash)] = (self._clock() + self._ttl, value)

    def evict(self, public_key: str) -> None:
        with self._lock:
            for k in [k for k in self._entries if k[0] == public_key]:
                del self._entries[k]
