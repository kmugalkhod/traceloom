"""Prompt composition: `@@@prompt:<name>|label=<l>@@@` / `|version=<n>@@@` (FR-P6).

Resolution is recursive (depth limit 5, cycle detection) and records the versions it used. The
lookup of a referenced prompt is injected, so this module never touches the database; each
distinct reference is looked up once per resolution. A resolution expands at most
`MAX_EXPANSIONS` includes and produces at most `MAX_EXPANDED_CHARS` characters, so a prompt
that includes another many times over several levels cannot blow up the server.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MAX_DEPTH = 5
MAX_EXPANSIONS = 100  # includes expanded in one resolution, repeats counted
MAX_EXPANDED_CHARS = 1_000_000

_REF_RE = re.compile(r"@@@prompt:([^\n@]*)@@@")
_NAME_RE = re.compile(r"[A-Za-z0-9_.\-/]{1,200}")
_LABEL_RE = re.compile(r"[a-z0-9_\-]{1,36}")


class CompositionError(ValueError):
    """A reference is malformed, missing, not a text prompt, too deep or circular."""

    def __init__(self, message: str, reference: str = "") -> None:
        super().__init__(message)
        self.reference = reference


@dataclass(frozen=True)
class Reference:
    name: str
    label: str | None
    version: int | None
    raw: str


@dataclass(frozen=True)
class Resolved:
    """What the injected resolver returns: one concrete version of a prompt, unresolved."""

    name: str
    version: int
    type: str  # text | chat
    content: Any
    label: str | None = None


Resolver = Callable[[Reference], Resolved | None]


def parse_reference(raw: str) -> Reference:
    """Parse the full `@@@prompt:...@@@` text of one reference."""
    match = _REF_RE.fullmatch(raw)
    if not match:
        raise CompositionError(f"Malformed prompt reference {raw}", raw)
    name, sep, selector = match.group(1).partition("|")
    if not _NAME_RE.fullmatch(name) or not sep:
        raise CompositionError(
            f"Malformed prompt reference {raw}: expected "
            "@@@prompt:<name>|label=<label>@@@ or @@@prompt:<name>|version=<n>@@@",
            raw,
        )
    key, _, value = selector.partition("=")
    if key == "label" and _LABEL_RE.fullmatch(value):
        return Reference(name, value, None, raw)
    if key == "version" and value.isdecimal() and int(value) >= 1:
        return Reference(name, None, int(value), raw)
    raise CompositionError(
        f"Malformed prompt reference {raw}: expected label=<label> or version=<n>", raw
    )


def find_references(text: str) -> list[Reference]:
    """Every reference in `text`, in order (duplicates kept)."""
    return [parse_reference(m.group(0)) for m in _REF_RE.finditer(text)]


def _dependency(resolved: Resolved) -> dict[str, Any]:
    return {"name": resolved.name, "version": resolved.version, "label": resolved.label}


def memoised(resolver: Resolver) -> Resolver:
    """`resolver` with each distinct reference looked up once."""
    seen: dict[tuple[str, str | None, int | None], Resolved | None] = {}

    def resolve(ref: Reference) -> Resolved | None:
        key = (ref.name, ref.label, ref.version)
        if key not in seen:
            seen[key] = resolver(ref)
        return seen[key]

    return resolve


class _Resolution:
    def __init__(self, resolver: Resolver, root: tuple[str, int] | None) -> None:
        self.resolver = memoised(resolver)
        self.root = root
        self.dependencies: list[dict[str, Any]] = []
        self.expansions = 0

    def text(self, text: str, stack: tuple[tuple[str, int], ...]) -> str:
        parts: list[str] = []
        size = pos = 0

        def add(piece: str) -> None:  # fails as soon as the running total passes the cap
            nonlocal size
            parts.append(piece)
            size += len(piece)
            if size > MAX_EXPANDED_CHARS:
                raise CompositionError(
                    f"The prompt is longer than {MAX_EXPANDED_CHARS:,} characters once its "
                    "references are expanded"
                )

        for match in _REF_RE.finditer(text):
            add(text[pos : match.start()])
            add(self._include(parse_reference(match.group(0)), stack))
            pos = match.end()
        add(text[pos:])
        return "".join(parts)

    def _include(self, ref: Reference, stack: tuple[tuple[str, int], ...]) -> str:
        # `stack` holds the prompts being expanded; the top-level content is depth 0.
        if len(stack) - (1 if self.root else 0) >= MAX_DEPTH:
            raise CompositionError(
                f"Prompt references are nested deeper than {MAX_DEPTH} levels at {ref.raw}",
                ref.raw,
            )
        self.expansions += 1
        if self.expansions > MAX_EXPANSIONS:
            raise CompositionError(
                f"The prompt expands to more than {MAX_EXPANSIONS} includes (counting repeats) "
                f"at {ref.raw}; include fewer prompts or nest them less",
                ref.raw,
            )
        resolved = self.resolver(ref)
        if resolved is None:
            raise CompositionError(f"Referenced prompt not found: {ref.raw}", ref.raw)
        if resolved.type != "text" or not isinstance(resolved.content, str):
            raise CompositionError(
                f"Only text prompts can be included; {ref.raw} is a {resolved.type} prompt",
                ref.raw,
            )
        key = (resolved.name, resolved.version)
        if key in stack:
            chain = " -> ".join(f"{n} v{v}" for n, v in (*stack, key))
            raise CompositionError(f"Circular prompt reference at {ref.raw} ({chain})", ref.raw)
        dep = _dependency(resolved)
        if dep not in self.dependencies:
            self.dependencies.append(dep)
        return self.text(resolved.content, (*stack, key))


def resolve_text(
    text: str, resolver: Resolver, *, root: tuple[str, int] | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Expand references in `text`. `root` is the (name, version) the text belongs to, so a
    prompt that includes itself is reported as a cycle. Returns (text, dependencies)."""
    run = _Resolution(resolver, root)
    out = run.text(text, (root,) if root else ())
    return out, run.dependencies


def resolve_messages(
    messages: list[dict[str, Any]], resolver: Resolver, *, root: tuple[str, int] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Expand references inside each chat message's string content."""
    run = _Resolution(resolver, root)
    stack = (root,) if root else ()
    out: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            message = {**message, "content": run.text(content, stack)}
        else:
            message = dict(message)
        out.append(message)
    return out, run.dependencies


@dataclass(frozen=True)
class Segment:
    """A piece of an expanded text: literal `text`, or an `include` with its own segments."""

    kind: str  # text | include
    text: str = ""
    ref: Reference | None = None
    resolved: Resolved | None = None
    children: tuple[Segment, ...] = ()


def expand_segments(
    text: str, resolver: Resolver, *, root: tuple[str, int] | None = None
) -> tuple[Segment, ...]:
    """Like `resolve_text`, but keeps the structure so a page can show where text came from.

    Raises CompositionError first for any missing include, cycle or excess depth.
    """
    resolver = memoised(resolver)
    resolve_text(text, resolver, root=root)  # validate; everything below is then safe

    def build(value: str) -> tuple[Segment, ...]:
        out: list[Segment] = []
        last = 0
        for match in _REF_RE.finditer(value):
            if match.start() > last:
                out.append(Segment("text", value[last : match.start()]))
            ref = parse_reference(match.group(0))
            found = resolver(ref)
            assert found is not None  # validated above
            out.append(Segment("include", ref=ref, resolved=found, children=build(found.content)))
            last = match.end()
        if last < len(value):
            out.append(Segment("text", value[last:]))
        return tuple(out)

    return build(text)


def resolve_content(
    prompt_type: str, content: Any, resolver: Resolver, *, root: tuple[str, int] | None = None
) -> tuple[Any, list[dict[str, Any]]]:
    if prompt_type == "chat":
        return resolve_messages(content, resolver, root=root)
    return resolve_text(content, resolver, root=root)
