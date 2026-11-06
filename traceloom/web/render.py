"""Rendering of trace input/output/metadata values for the IO viewer.

Every function returns `Markup` that is already safe: user text is escaped, and Markdown is
rendered with raw HTML disabled and unsafe link schemes refused. Templates can print the
results without `|safe` games and without double escaping.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from markdown_it import MarkdownIt
from markupsafe import Markup, escape

MODES = ("formatted", "json", "markdown")
_KNOWN_ROLES = {"system", "user", "assistant", "tool", "function", "developer"}


def _dump(value: Any, **kwargs: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, **kwargs)


def pretty_json(value: Any) -> str:
    """Indented JSON for structures; strings pass through unquoted. Plain text, unescaped."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return _dump(value, indent=2, sort_keys=False)


def raw_json(value: Any) -> str:
    """Compact JSON of the value exactly as stored. Plain text, not escaped."""
    return _dump(value)


def is_chat(value: Any) -> bool:
    """A non-empty list of {role, content} messages."""
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(m, dict) and isinstance(m.get("role"), str) and "content" in m for m in value
        )
    )


@dataclass(frozen=True)
class ChatMessage:
    role: str  # as sent, escaped by the template or by `html`
    css_role: str  # one of the known roles or "other": safe to use in a class name
    html: Markup  # escaped content


@dataclass(frozen=True)
class Rendered:
    kind: str  # chat | text | json | markdown | empty
    html: Markup = Markup("")
    messages: tuple[ChatMessage, ...] = ()


def chat_messages(value: list[dict[str, Any]]) -> tuple[ChatMessage, ...]:
    out = []
    for message in value:
        role = message["role"]
        out.append(
            ChatMessage(
                role=role,
                css_role=role if role in _KNOWN_ROLES else "other",
                html=escape(pretty_json(message.get("content"))),
            )
        )
    return tuple(out)


def _markdown_engine() -> MarkdownIt:
    # commonmark with raw HTML off. markdown-it already refuses javascript:, vbscript:, file:
    # and data: link targets. Images are disabled so no remote request is made while viewing.
    md = MarkdownIt("commonmark", {"html": False, "linkify": False}).disable("image")

    def link_open(tokens, idx, options, env):
        tokens[idx].attrSet("rel", "noopener noreferrer nofollow")
        tokens[idx].attrSet("target", "_blank")
        return md.renderer.renderToken(tokens, idx, options, env)

    md.renderer.rules["link_open"] = link_open
    return md


_MD = _markdown_engine()


def render_markdown(value: Any) -> Markup:
    if value is None or value == "":
        return Markup("")
    if not isinstance(value, str):
        return Markup("<pre>{}</pre>").format(pretty_json(value))
    return Markup(_MD.render(value))


def render_value(value: Any, mode: str = "formatted") -> Rendered:
    """The IO viewer body for one value in one of MODES."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    if value is None or value == "" or value == [] or value == {}:
        return Rendered("empty")
    if mode == "json":
        return Rendered("json", Markup("<pre>{}</pre>").format(raw_json(value)))
    if mode == "markdown":
        return Rendered("markdown", render_markdown(value))
    if is_chat(value):
        return Rendered("chat", messages=chat_messages(value))
    return Rendered("text", Markup("<pre>{}</pre>").format(pretty_json(value)))
