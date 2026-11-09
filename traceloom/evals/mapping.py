"""Variable mapping for judge templates (FR-J3): a small JSONPath subset and variable resolution.

Paths support `$`, `.name` and `[n]` (negative indexes count from the end), such as `$.a.b[0]` or
`$.messages[-1].content`. Nothing is ever evaluated: a path is parsed into plain steps and walked.

A rule's mapping is `{variable: {"source": ..., "path": ..., "default": ...}}`. Sources:
`input`, `output`, `expected_output`, `metadata.<key>`, and `observation` (a child observation
picked by `"observation": <name>` and `"field": "input" | "output"`). `path` and `default` are
optional. `input`, `output` and `expected_output` map to themselves when a variable has no entry.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_NAME = re.compile(r"[A-Za-z0-9_\-]+")
_INDEX = re.compile(r"\[(-?\d+)\]")
IMPLICIT_SOURCES = ("input", "output", "expected_output")
OBSERVATION_FIELDS = ("input", "output")


class PathError(ValueError):
    """The path is not valid JSONPath-subset syntax."""


class MappingError(ValueError):
    """A variable cannot be resolved; the message names the variable and is safe to show."""


Step = str | int


def parse_path(path: str) -> list[Step]:
    if not isinstance(path, str) or not path.startswith("$"):
        raise PathError(f"path {path!r} must start with $")
    steps: list[Step] = []
    pos = 1
    while pos < len(path):
        if path[pos] == ".":
            match = _NAME.match(path, pos + 1)
            if not match:
                raise PathError(f"path {path!r}: expected a field name at position {pos + 1}")
            steps.append(match.group())
        elif path[pos] == "[":
            match = _INDEX.match(path, pos)
            if not match:
                raise PathError(f"path {path!r}: only integer indexes are supported in [ ]")
            steps.append(int(match.group(1)))
        else:
            raise PathError(f"path {path!r}: unexpected {path[pos]!r} at position {pos}")
        pos = match.end()
    return steps


class _Missing(Exception):
    pass


def _walk(value: Any, steps: Sequence[Step]) -> Any:
    for step in steps:
        if isinstance(value, str):
            try:
                value = json.loads(value)  # a JSON document stored as text
            except ValueError:
                raise _Missing from None
        if isinstance(step, int):
            if not isinstance(value, list) or not -len(value) <= step < len(value):
                raise _Missing
            value = value[step]
        else:
            if not isinstance(value, dict) or step not in value:
                raise _Missing
            value = value[step]
    return value


def resolve_path(value: Any, path: str) -> Any:
    """The value at `path` inside `value`; raises MappingError when it is not there."""
    try:
        return _walk(value, parse_path(path))
    except _Missing:
        raise MappingError(f"path {path} not found") from None
    except PathError as exc:
        raise MappingError(str(exc)) from exc


@dataclass
class ObjectContext:
    """What a rule's mapping can read from the evaluated object."""

    input: Any = None
    output: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    expected_output: Any = None
    observations: Sequence[Mapping[str, Any]] = ()  # {"name", "input", "output"}, time order


def _source_value(spec: Mapping[str, Any], ctx: ObjectContext) -> Any:
    source = spec.get("source")
    if source in IMPLICIT_SOURCES:
        value = getattr(ctx, source)
    elif isinstance(source, str) and source.startswith("metadata."):
        key = source[len("metadata.") :]
        if key not in ctx.metadata:
            raise MappingError(f"metadata has no key {key!r}")
        value = ctx.metadata[key]
    elif source == "observation":
        name, field_ = spec.get("observation"), spec.get("field", "output")
        if field_ not in OBSERVATION_FIELDS:
            raise MappingError(f"observation field must be one of {OBSERVATION_FIELDS}")
        matches = [o for o in ctx.observations if o.get("name") == name]
        if not matches:
            raise MappingError(f"no child observation named {name!r}")
        value = matches[-1].get(field_)  # the latest one when a name repeats
    else:
        raise MappingError(f"unknown source {source!r}")
    if value is None:
        raise MappingError(f"{source} is empty")
    return value


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def resolve_variables(
    variables: Sequence[str], mapping: Mapping[str, Any] | None, ctx: ObjectContext
) -> dict[str, str]:
    """A text value for every variable. Raises MappingError naming the first one that cannot be
    resolved and has no default."""
    mapping = mapping or {}
    values: dict[str, str] = {}
    for name in variables:
        spec = mapping.get(name)
        if spec is None:
            if name not in IMPLICIT_SOURCES:
                raise MappingError(f"variable {name!r} has no mapping")
            spec = {"source": name}
        try:
            value = _source_value(spec, ctx)
            if spec.get("path"):
                value = resolve_path(value, spec["path"])
            if value is None:
                raise MappingError("value is null")
        except MappingError as exc:
            if "default" in spec:
                values[name] = _as_text(spec["default"])
                continue
            raise MappingError(f"variable {name!r}: {exc}") from exc
        values[name] = _as_text(value)
    return values
