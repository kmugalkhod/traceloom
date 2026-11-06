"""Prompt fetching for the SDK: `TextPrompt`, `ChatPrompt`, a TTL cache and `get_prompt`.

`get_prompt` is the one SDK call allowed to raise (`PromptNotFound`), and only when there is
neither a cached value nor a fallback: the caller has nothing to run otherwise. Everything else
here, background refreshes included, swallows its errors so a prompt problem never breaks the
host app. Only the standard library and the client's own httpx client are used.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from traceloom_sdk.client import Traceloom

log = logging.getLogger("traceloom")

PROMPTS_PATH = "/api/public/v2/prompts"
DEFAULT_TTL_SECONDS = 60.0
MAX_CACHE_ENTRIES = 256
_RETRY_STATUS = {429, 500, 502, 503, 504}
_VARIABLE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class PromptNotFound(Exception):
    """The prompt could not be fetched and there was no cached value or fallback."""


def _substitute(text: str, values: Mapping[str, Any]) -> str:
    """Known variables are replaced in one pass; unknown ones are left as they are."""
    return _VARIABLE_RE.sub(
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text
    )


class _Prompt:
    type = ""

    def __init__(
        self,
        name: str,
        prompt: Any,
        *,
        version: int | None = None,
        config: dict[str, Any] | None = None,
        labels: list[str] | None = None,
        tags: list[str] | None = None,
        commit_message: str = "",
        dependencies: list[dict[str, Any]] | None = None,
        is_fallback: bool = False,
    ) -> None:
        self.name = name
        self.prompt = prompt
        self.version = version
        self.config = config or {}
        self.labels = labels or []
        self.tags = tags or []
        self.commit_message = commit_message
        self.dependencies = dependencies or []
        self.is_fallback = is_fallback

    @property
    def variables(self) -> list[str]:
        raise NotImplementedError

    def compile(self, **variables: Any) -> Any:
        raise NotImplementedError

    def __repr__(self) -> str:
        suffix = " fallback" if self.is_fallback else ""
        return f"<{type(self).__name__} {self.name!r} v{self.version}{suffix}>"


class TextPrompt(_Prompt):
    type = "text"

    @property
    def variables(self) -> list[str]:
        return list(dict.fromkeys(_VARIABLE_RE.findall(self.prompt)))

    def compile(self, **variables: Any) -> str:
        return _substitute(self.prompt, variables)


class ChatPrompt(_Prompt):
    type = "chat"

    @property
    def variables(self) -> list[str]:
        names: list[str] = []
        for message in self.prompt:
            if message.get("role") != "placeholder":
                names.extend(_VARIABLE_RE.findall(str(message.get("content", ""))))
        return list(dict.fromkeys(names))

    def compile(self, **variables: Any) -> list[dict[str, Any]]:
        out = []
        for message in self.prompt:
            message = dict(message)
            if message.get("role") != "placeholder" and isinstance(message.get("content"), str):
                message["content"] = _substitute(message["content"], variables)
            out.append(message)
        return out


def prompt_from_dict(data: Mapping[str, Any]) -> TextPrompt | ChatPrompt:
    """Build a prompt from a v2 API response body."""
    cls = ChatPrompt if data.get("type") == "chat" else TextPrompt
    return cls(
        data["name"],
        data["prompt"],
        version=data.get("version"),
        config=data.get("config"),
        labels=data.get("labels"),
        tags=data.get("tags"),
        commit_message=data.get("commitMessage", ""),
        dependencies=data.get("dependencies"),
    )


def fallback_prompt(name: str, fallback: Any, type: str | None = None) -> TextPrompt | ChatPrompt:
    """A prompt built from the caller's fallback (a str, or a list of chat messages)."""
    if type == "chat" or (type is None and isinstance(fallback, list)):
        return ChatPrompt(name, list(fallback), is_fallback=True)
    return TextPrompt(name, str(fallback), is_fallback=True)


class PromptCache:
    """In-process cache with a TTL. Stale values are served while one thread per key refreshes."""

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, max_entries: int = MAX_CACHE_ENTRIES
    ) -> None:
        self._clock = clock
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: dict[Any, tuple[float, Any]] = {}
        self.refreshers: dict[Any, threading.Thread] = {}

    def get(self, key: Any, ttl: float) -> tuple[Any, bool] | None:
        """(prompt, is_fresh), or None when nothing is cached."""
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, prompt = entry
        return prompt, (self._clock() - stored_at) < ttl

    def put(self, key: Any, prompt: Any) -> None:
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = (self._clock(), prompt)
            while len(self._entries) > self._max:  # drop the oldest
                self._entries.pop(next(iter(self._entries)))

    def start_refresh(self, key: Any, work: Callable[[], None]) -> bool:
        """Run `work` on a daemon thread unless this key already has one. Never raises."""

        def run() -> None:
            try:
                work()
            except Exception:
                log.debug("prompt refresh failed for %r", key, exc_info=True)
            finally:
                with self._lock:
                    self.refreshers.pop(key, None)

        with self._lock:
            if key in self.refreshers:
                return False
            thread = threading.Thread(target=run, name="traceloom-prompt-refresh", daemon=True)
            self.refreshers[key] = thread
        try:
            thread.start()
        except Exception:
            with self._lock:
                self.refreshers.pop(key, None)
            return False
        return True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def _ttl(client: Traceloom, cache_ttl_seconds: float | None) -> float:
    if cache_ttl_seconds is not None:
        return max(0.0, float(cache_ttl_seconds))
    from traceloom_sdk.client import _env_number

    return max(0.0, _env_number("TRACELOOM_PROMPT_CACHE_TTL", DEFAULT_TTL_SECONDS, float))


def fetch_prompt(
    client: Traceloom,
    name: str,
    label: str | None,
    version: int | None,
    max_retries: int,
    timeout: float,
) -> TextPrompt | ChatPrompt | None:
    """GET the prompt. None on any failure. Total time is bounded by timeout x (retries + 1)."""
    if not (client.public_key and client.secret_key):
        return None
    params: dict[str, Any] = {}
    if version is not None:
        params["version"] = version
    elif label is not None:
        params["label"] = label
    url = f"{client.host}{PROMPTS_PATH}/{quote(name, safe='')}"
    deadline = time.monotonic() + timeout * (max_retries + 1)
    for attempt in range(max_retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            response = client._client().get(url, params=params, timeout=min(timeout, remaining))
            if response.status_code == 200:
                return prompt_from_dict(response.json())
            if response.status_code not in _RETRY_STATUS:
                log.warning("Traceloom prompt %r not fetched: HTTP %d", name, response.status_code)
                return None
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.debug("prompt fetch %r failed: %s", name, exc)
        except Exception:
            log.debug("prompt fetch %r failed", name, exc_info=True)
        pause = min(0.1 * (2**attempt), max(0.0, deadline - time.monotonic()))
        if attempt < max_retries and pause > 0:
            time.sleep(pause)
    return None


def get_prompt(
    client: Traceloom,
    name: str,
    *,
    label: str | None = None,
    version: int | None = None,
    type: str | None = None,
    cache_ttl_seconds: float | None = None,
    fallback: Any = None,
    max_retries: int = 2,
    fetch_timeout_seconds: float = 5,
) -> TextPrompt | ChatPrompt:
    """The prompt by label (default `production`, else latest) or version, cached per client."""
    ttl = _ttl(client, cache_ttl_seconds)
    key = (name, version if version is not None else label)
    cache: PromptCache = client._prompt_cache

    def fetch() -> TextPrompt | ChatPrompt | None:
        return fetch_prompt(client, name, label, version, max_retries, fetch_timeout_seconds)

    def refresh() -> None:
        fresh = fetch()
        if fresh is not None:
            cache.put(key, fresh)

    if ttl > 0:
        cached = cache.get(key, ttl)
        if cached is not None:
            prompt, is_fresh = cached
            if not is_fresh:
                cache.start_refresh(key, refresh)  # serve the stale value right away
            return prompt

    prompt = fetch()
    if prompt is not None:
        if ttl > 0:
            cache.put(key, prompt)
        return prompt
    if fallback is not None:
        return fallback_prompt(name, fallback, type)
    raise PromptNotFound(
        f"Prompt {name!r} could not be fetched and no cached value or fallback is available."
    )
