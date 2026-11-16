from __future__ import annotations

import pytest

from traceloom.auth.permissions import Role
from traceloom.datasets import importer, service
from traceloom.datasets.importer import ImportLimitError, Mapping

CSV = (
    b"question,answer,topic\n"
    b'"{""a"": 1}",hello,geo\n'
    b'"{broken",world,\n'
    b"plain text,,math\n"
    b",nothing,x\n"
    b'"<script>alert(1)</script>",ok,x\n'
)


def table(data=CSV, fmt="csv", **kw):
    kw.setdefault("max_rows", 100)
    return importer.read_table(data, fmt, **kw)


MAPPING = Mapping("question", "answer", ["topic"])


def test_csv_malformed_json_kept_as_string_with_warning():
    pv = importer.preview(table(), MAPPING)
    rows = {r.number: r for r in pv.rows}
    assert rows[1].input == {"a": 1} and rows[1].warnings == []
    assert rows[2].input == "{broken" and rows[2].ok
    assert rows[2].warnings == ["question: not valid JSON, kept as text."]
    assert rows[3].input == "plain text" and rows[3].warnings == []
    assert not rows[4].ok and "input is empty" in rows[4].errors[0]
    assert (pv.total, pv.error_count, pv.warning_count) == (5, 1, 1)
    assert rows[1].metadata == {"topic": "geo"} and rows[2].metadata == {}


def test_preview_is_first_five_rows_only():
    data = "input\n" + "\n".join(f"row {i}" for i in range(20))
    pv = importer.preview(table(data.encode()), Mapping("input"))
    assert (pv.total, len(pv.rows)) == (20, 5)


def test_row_limit():
    data = "input\n" + "\n".join(f"r{i}" for i in range(11))
    with pytest.raises(ImportLimitError, match="more than 10 rows"):
        table(data.encode(), max_rows=10)
    table(data.encode(), max_rows=11)  # exactly at the limit is fine
    with pytest.raises(ImportLimitError):
        table(b'[{"input": 1}, {"input": 2}]', "json", max_rows=1)
    with pytest.raises(ImportLimitError):
        table(b'{"input": 1}\n{"input": 2}\n', "jsonl", max_rows=1)


def test_size_limit_and_bad_files():
    with pytest.raises(ImportLimitError, match="upload limit"):
        table(b"input\nx\n", max_bytes=3)
    for data, fmt in ((b"", "csv"), (b"input\n", "csv"), (b"\xff\xfe", "csv"), (b"nope", "json")):
        with pytest.raises(ImportLimitError):
            table(data, fmt)
    with pytest.raises(ImportLimitError):
        table(b'["a"]', "json")
    with pytest.raises(ImportLimitError, match="Line 2"):
        table(b'{"input": 1}\n{oops\n', "jsonl")
    with pytest.raises(ImportLimitError):
        table(b"a,a\n1,2\n")  # duplicate column names


def test_json_and_jsonl_values_are_typed():
    obj = b'[{"in": {"q": 1}, "out": [1, 2], "meta": {"k": "v"}}, {"in": "text"}]'
    for data, fmt in (
        (obj, "json"),
        (b'{"in": {"q": 1}, "out": [1, 2], "meta": {"k": "v"}}\n{"in": "text"}\n', "jsonl"),
    ):
        rows = importer.map_table(table(data, fmt), Mapping("in", "out", ["meta"]))
        assert (rows[0].input, rows[0].expected_output, rows[0].metadata) == (
            {"q": 1},
            [1, 2],
            {"k": "v"},
        )
        assert rows[1].input == "text" and rows[1].expected_output is None


def test_mapping_must_name_real_columns():
    with pytest.raises(service.DatasetError, match="no column named 'nope'"):
        importer.preview(table(), Mapping("nope"))
    with pytest.raises(service.DatasetError):
        importer.preview(table(), Mapping(""))
    assert importer.default_mapping(["Input", "Expected", "x"]) == Mapping("Input", "Expected", [])
    assert importer.default_mapping(["a"]) is None


def test_script_cell_stays_literal_text():
    pv = importer.preview(table(), MAPPING)
    assert pv.rows[4].input == "<script>alert(1)</script>"


def test_import_saves_valid_rows(db, world):
    member = service.Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)
    dataset = service.create_dataset(db, world.project, member, name="imp")
    result = importer.import_rows(db, world.project, member, dataset, table(), MAPPING)
    db.commit()
    assert (result.created, [r.number for r in result.skipped]) == (4, [4])
    assert [r.number for r in result.warnings] == [2]
    items = service.items_as_of(db, world.project, dataset)
    assert [i.input for i in items][:2] == [{"a": 1}, "{broken"]
    assert items[0].expected_output == "hello" and items[0].version == 1
