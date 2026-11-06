"""Diffs between two prompt versions (FR-P5): lines, chat messages and config keys.

Every text field in the result is a `markupsafe.Markup` that has already been escaped, so the
output holds no raw HTML and a template can print it as-is (without a second escape).
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from typing import Any

from markupsafe import Markup, escape


@dataclass(frozen=True)
class DiffLine:
    op: str  # equal | add | remove
    text: Markup
    old_no: int | None = None
    new_no: int | None = None


@dataclass(frozen=True)
class MessageDiff:
    index: int
    status: str  # unchanged | changed | added | removed
    old_role: Markup | None
    new_role: Markup | None
    lines: list[DiffLine] = field(default_factory=list)

    @property
    def role_changed(self) -> bool:
        return (
            self.old_role is not None
            and self.new_role is not None
            and (self.old_role != self.new_role)
        )


@dataclass(frozen=True)
class KeyDiff:
    key: Markup
    status: str  # unchanged | changed | added | removed
    old: Markup | None
    new: Markup | None


@dataclass(frozen=True)
class PromptDiff:
    content: list[DiffLine] | list[MessageDiff]
    config: list[KeyDiff]

    @property
    def changed(self) -> bool:
        content_changed = any(
            (isinstance(c, MessageDiff) and c.status != "unchanged")
            or (isinstance(c, DiffLine) and c.op != "equal")
            for c in self.content
        )
        return content_changed or any(k.status != "unchanged" for k in self.config)


def _lines(value: str) -> list[str]:
    return value.splitlines()


def diff_text(old: str, new: str) -> list[DiffLine]:
    """A line diff. Removed lines come before the lines that replace them."""
    a, b = _lines(old), _lines(new)
    out: list[DiffLine] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            out.extend(
                DiffLine("equal", escape(a[i]), i + 1, j1 + (i - i1) + 1) for i in range(i1, i2)
            )
            continue
        out.extend(DiffLine("remove", escape(a[i]), old_no=i + 1) for i in range(i1, i2))
        out.extend(DiffLine("add", escape(b[j]), new_no=j + 1) for j in range(j1, j2))
    return out


def _text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    return content if isinstance(content, str) else json.dumps(content, sort_keys=True)


def diff_chat(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[MessageDiff]:
    """Message by message, matched by position."""
    out: list[MessageDiff] = []
    for i in range(max(len(old), len(new))):
        a = old[i] if i < len(old) else None
        b = new[i] if i < len(new) else None
        if a is None:
            assert b is not None
            out.append(
                MessageDiff(i, "added", None, escape(b.get("role", "")), diff_text("", _text(b)))
            )
        elif b is None:
            out.append(
                MessageDiff(i, "removed", escape(a.get("role", "")), None, diff_text(_text(a), ""))
            )
        else:
            lines = diff_text(_text(a), _text(b))
            same = a.get("role") == b.get("role") and all(line.op == "equal" for line in lines)
            out.append(
                MessageDiff(
                    i,
                    "unchanged" if same else "changed",
                    escape(a.get("role", "")),
                    escape(b.get("role", "")),
                    lines,
                )
            )
    return out


def _json(value: Any) -> Markup:
    return escape(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str))


def diff_config(old: dict[str, Any], new: dict[str, Any]) -> list[KeyDiff]:
    """Key-level: each top-level key is added, removed, changed or unchanged."""
    out: list[KeyDiff] = []
    for key in sorted({*old, *new}):
        if key not in old:
            out.append(KeyDiff(escape(key), "added", None, _json(new[key])))
        elif key not in new:
            out.append(KeyDiff(escape(key), "removed", _json(old[key]), None))
        else:
            status = "unchanged" if old[key] == new[key] else "changed"
            out.append(KeyDiff(escape(key), status, _json(old[key]), _json(new[key])))
    return out


def diff_versions(
    prompt_type: str,
    old_content: Any,
    new_content: Any,
    old_config: dict[str, Any],
    new_config: dict[str, Any],
) -> PromptDiff:
    content = (
        diff_chat(old_content, new_content)
        if prompt_type == "chat"
        else diff_text(old_content, new_content)
    )
    return PromptDiff(content, diff_config(old_config, new_config))
