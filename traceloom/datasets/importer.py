"""Dataset import (FR-D3): CSV, JSON and JSONL into items, with column mapping and a preview.

`read_table` turns an upload into records (rejecting files over the size or row limit),
`preview` maps them to items without writing anything and reports per-row errors and warnings,
and `import_rows` saves the valid rows as items through the dataset service.

A CSV cell is a JSON value when it starts with `{`, `[` or `"` and parses; a cell that looks
like JSON but does not parse is kept as a string with a warning. Any other cell is plain text.
JSON and JSONL values are already typed and are used as they are.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from traceloom.datasets import service
from traceloom.datasets.service import Actor
from traceloom.db.models import Project
from traceloom.db.models_datasets import Dataset

FORMATS = ("csv", "json", "jsonl")
PREVIEW_ROWS = 5
_JSON_STARTS = ("{", "[", '"')


class ImportLimitError(service.DatasetError):
    """The file is over the size or row limit, or cannot be read at all."""


@dataclass
class Mapping:
    """Which column feeds which field. `input` is required; `metadata` takes any columns."""

    input: str
    expected_output: str | None = None
    metadata: list[str] = field(default_factory=list)


@dataclass
class ParsedRow:
    number: int  # 1-based position of the record in the file
    input: Any = None
    expected_output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class Table:
    columns: list[str]
    records: list[dict[str, Any]]
    format: str


@dataclass
class Preview:
    columns: list[str]
    total: int
    rows: list[ParsedRow]  # the first PREVIEW_ROWS rows
    error_count: int
    warning_count: int


@dataclass
class ImportResult:
    created: int
    skipped: list[ParsedRow]
    warnings: list[ParsedRow]


def detect_format(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in FORMATS:
        return ext
    if ext == "ndjson":
        return "jsonl"
    raise ImportLimitError("Upload a .csv, .json or .jsonl file.")


def read_table(data: bytes, fmt: str, *, max_rows: int, max_bytes: int | None = None) -> Table:
    """Parse an upload. Raises ImportLimitError over the limits or on an unreadable file."""
    if fmt not in FORMATS:
        raise ImportLimitError("Upload a .csv, .json or .jsonl file.")
    if max_bytes is not None and len(data) > max_bytes:
        raise ImportLimitError(f"The file is over the {max_bytes:,} byte upload limit.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportLimitError("The file must be UTF-8 text.") from exc
    reader = {"csv": _read_csv, "json": _read_json, "jsonl": _read_jsonl}[fmt]
    columns, records = reader(text, max_rows)
    if not records:
        raise ImportLimitError("The file has no rows.")
    return Table(columns, records, fmt)


def _too_many(max_rows: int) -> ImportLimitError:
    return ImportLimitError(f"The file has more than {max_rows:,} rows; split it and import again.")


def _read_csv(text: str, max_rows: int) -> tuple[list[str], list[dict[str, Any]]]:
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise ImportLimitError("The file is empty.") from None
    except csv.Error as exc:
        raise ImportLimitError(f"The CSV could not be read: {exc}") from exc
    columns = [c.strip() for c in header]
    if not any(columns) or len(set(columns)) != len(columns):
        raise ImportLimitError("The first row must name each column, with no duplicates.")
    records: list[dict[str, Any]] = []
    try:
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue  # blank line
            if len(records) >= max_rows:
                raise _too_many(max_rows)
            records.append(dict(zip(columns, row, strict=False)))
    except csv.Error as exc:
        raise ImportLimitError(f"The CSV could not be read: {exc}") from exc
    return columns, records


def _columns_of(records: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for record in records:
        seen.update(dict.fromkeys(record))
    return list(seen)


def _objects(values: list[Any], max_rows: int) -> tuple[list[str], list[dict[str, Any]]]:
    if len(values) > max_rows:
        raise _too_many(max_rows)
    if not all(isinstance(v, dict) for v in values):
        raise ImportLimitError("Every row must be a JSON object.")
    return _columns_of(values), values


def _read_json(text: str, max_rows: int) -> tuple[list[str], list[dict[str, Any]]]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportLimitError(
            f"The file is not valid JSON: {exc.msg} (line {exc.lineno})."
        ) from exc
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise ImportLimitError("A JSON file must hold an array of objects.")
    return _objects(value, max_rows)


def _read_jsonl(text: str, max_rows: int) -> tuple[list[str], list[dict[str, Any]]]:
    values: list[Any] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if len(values) >= max_rows:
            raise _too_many(max_rows)
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ImportLimitError(f"Line {number} is not valid JSON: {exc.msg}.") from exc
    return _objects(values, max_rows)


def default_mapping(columns: list[str]) -> Mapping | None:
    """A starting mapping by column name (input, expected_output / expected, metadata)."""
    lower = {c.lower(): c for c in columns}
    first = next((lower[k] for k in ("input", "question", "prompt") if k in lower), None)
    if first is None:
        return None
    expected = next(
        (lower[k] for k in ("expected_output", "expected", "output", "answer") if k in lower),
        None,
    )
    metadata = [lower["metadata"]] if "metadata" in lower else []
    return Mapping(first, expected, metadata)


def parse_cell(value: Any, label: str, warnings: list[str]) -> Any:
    """A CSV cell becomes JSON when it parses and a string otherwise (warning if it looks
    like JSON). Non-string values (from JSON files) pass through."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.startswith(_JSON_STARTS):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            warnings.append(f"{label}: not valid JSON, kept as text.")
    return value


def map_record(number: int, record: dict[str, Any], mapping: Mapping, fmt: str) -> ParsedRow:
    row = ParsedRow(number)
    csv_cells = fmt == "csv"

    def cell(column: str) -> Any:
        value = record.get(column)
        return parse_cell(value, column, row.warnings) if csv_cells else value

    row.input = cell(mapping.input)
    if row.input is None or row.input == "":
        row.errors.append(f"{mapping.input}: input is empty.")
    if mapping.expected_output:
        row.expected_output = cell(mapping.expected_output)
    for column in mapping.metadata:
        value = cell(column)
        if value is None:
            continue
        if isinstance(value, dict) and len(mapping.metadata) == 1:
            row.metadata = value  # one metadata column holding an object is the metadata
        else:
            row.metadata[column] = value
    try:
        service._json_size_ok(row.input, "Input")
        service._json_size_ok(row.expected_output, "Expected output")
    except service.DatasetError as exc:
        row.errors.append(str(exc))
    return row


def _check_mapping(table: Table, mapping: Mapping) -> None:
    if not mapping.input:
        raise service.DatasetError("Choose the column that holds the input.")
    for column in [mapping.input, mapping.expected_output, *mapping.metadata]:
        if column and column not in table.columns:
            raise service.DatasetError(f"The file has no column named {column!r}.")


def map_table(table: Table, mapping: Mapping) -> list[ParsedRow]:
    _check_mapping(table, mapping)
    return [
        map_record(number, record, mapping, table.format)
        for number, record in enumerate(table.records, start=1)
    ]


def preview(table: Table, mapping: Mapping) -> Preview:
    rows = map_table(table, mapping)
    return Preview(
        columns=table.columns,
        total=len(rows),
        rows=rows[:PREVIEW_ROWS],
        error_count=sum(1 for r in rows if r.errors),
        warning_count=sum(len(r.warnings) for r in rows),
    )


def import_rows(
    db: Session,
    project: Project,
    actor: Actor,
    dataset: Dataset,
    table: Table,
    mapping: Mapping,
) -> ImportResult:
    """Save every valid row as an item; rows with errors are skipped and reported."""
    rows = map_table(table, mapping)
    created = 0
    for row in rows:
        if not row.ok:
            continue
        service.create_item(
            db,
            project,
            actor,
            dataset,
            input=row.input,
            expected_output=row.expected_output,
            metadata=row.metadata,
        )
        created += 1
    return ImportResult(
        created, [r for r in rows if not r.ok], [r for r in rows if r.warnings and r.ok]
    )
