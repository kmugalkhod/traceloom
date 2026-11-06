"""`{{name}}` variables (FR-P3). Unknown variables are left as-is and reported, never an error."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

VARIABLE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


@dataclass(frozen=True)
class Compiled:
    text: str
    missing: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CompiledMessages:
    messages: list[dict[str, Any]]
    missing: list[str] = field(default_factory=list)


def find_variables(text: str) -> list[str]:
    """Distinct variable names in order of first appearance."""
    return list(dict.fromkeys(VARIABLE_RE.findall(text)))


def find_message_variables(messages: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and message.get("role") != "placeholder":
            names.extend(find_variables(content))
    return list(dict.fromkeys(names))


def compile_text(text: str, values: Mapping[str, Any]) -> Compiled:
    """Substitute known variables in one pass (substituted values are not re-scanned)."""
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in values:
            return str(values[name])
        if name not in missing:
            missing.append(name)
        return match.group(0)

    return Compiled(VARIABLE_RE.sub(replace, text), missing)


def compile_messages(messages: list[dict[str, Any]], values: Mapping[str, Any]) -> CompiledMessages:
    """Substitute in every message's content. Placeholder messages pass through untouched."""
    out: list[dict[str, Any]] = []
    missing: list[str] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "placeholder" or not isinstance(content, str):
            out.append(dict(message))
            continue
        result = compile_text(content, values)
        missing.extend(n for n in result.missing if n not in missing)
        out.append({**message, "content": result.text})
    return CompiledMessages(out, missing)
