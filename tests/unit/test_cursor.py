from datetime import UTC, datetime

import pytest

from traceloom.ports import InvalidCursor, decode_cursor, encode_cursor


@pytest.mark.parametrize(
    "value",
    ["abc", 12, 3.5, None, datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)],
)
def test_round_trip(value):
    token = encode_cursor(value, "trace-1")
    assert decode_cursor(token) == (value, "trace-1")


def test_tampered_cursor_rejected():
    token = encode_cursor("abc", "trace-1")
    flipped = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    with pytest.raises(InvalidCursor):
        decode_cursor(flipped)


@pytest.mark.parametrize("bad", ["", "!!!", "e30", "bm90LWpzb24uYWJj"])
def test_garbage_rejected(bad):
    with pytest.raises(InvalidCursor):
        decode_cursor(bad)
