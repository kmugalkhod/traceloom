"""Presentation helpers for prompt pages: escaped highlighting and the resolved preview tree.

Everything returned as `Markup` has been escaped piece by piece, so templates print it as-is and
user text can never become markup.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from markupsafe import Markup, escape

from traceloom.prompts.composition import Segment
from traceloom.prompts.variables import VARIABLE_RE
from traceloom.web.templating import templates

_TOKEN = re.compile(r"\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}|@@@prompt:[^\n@]*@@@")


def highlight(text: str) -> Markup:
    """Escaped text with `{{variables}}` and `@@@prompt:...@@@` includes wrapped for styling."""
    out: list[Markup] = []
    last = 0
    for match in _TOKEN.finditer(text):
        out.append(escape(text[last : match.start()]))
        css = "var" if match.group(0).startswith("{") else "inc"
        out.append(Markup('<span class="{}">{}</span>').format(css, match.group(0)))
        last = match.end()
    out.append(escape(text[last:]))
    return Markup("").join(out)


def fill(text: str, values: Mapping[str, str]) -> tuple[Markup, list[str]]:
    """Substitute filled variables (escaped, plain) and highlight the ones left empty.

    Returns the markup and the names of variables that had no value.
    """
    missing: list[str] = []
    out: list[Markup] = []
    last = 0
    for match in VARIABLE_RE.finditer(text):
        out.append(escape(text[last : match.start()]))
        name = match.group(1)
        if values.get(name):
            out.append(escape(values[name]))
        else:
            if name not in missing:
                missing.append(name)
            out.append(Markup('<span class="var">{}</span>').format(match.group(0)))
        last = match.end()
    out.append(escape(text[last:]))
    return Markup("").join(out), missing


def preview_parts(
    segments: tuple[Segment, ...], values: Mapping[str, str], missing: list[str]
) -> list[dict[str, Any]]:
    """The expanded segments as a tree the template can walk: text parts and include blocks."""
    parts: list[dict[str, Any]] = []
    for segment in segments:
        if segment.kind == "text":
            html, absent = fill(segment.text, values)
            missing.extend(n for n in absent if n not in missing)
            parts.append({"kind": "text", "html": html})
        else:
            assert segment.ref is not None and segment.resolved is not None
            via = f"label {segment.ref.label}" if segment.ref.label else "pinned version"
            parts.append(
                {
                    "kind": "include",
                    "name": segment.resolved.name,
                    "version": segment.resolved.version,
                    "via": via,
                    "children": preview_parts(segment.children, values, missing),
                }
            )
    return parts


templates.env.filters["hl"] = highlight
