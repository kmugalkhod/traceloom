"""Acceptance scenarios AC-3 for trace search and filters, run against the lite read store.

The HTTP routes arrive with the trace explorer UI task; these tests pin the query behaviour
they will call.
"""

from __future__ import annotations

from tests.contract.stores import minutes
from traceloom.ports import TraceQuery

PROJECT = "proj-ac3"


def test_search_output_word(read_store, seed):
    seed(PROJECT, "t1", output="the invoice was paid")
    seed(PROJECT, "t2", output="a zebra crossed the road")
    seed(PROJECT, "t3", output="nothing to see")
    page = read_store.list_traces(PROJECT, TraceQuery(q="zebra"))
    assert [t.id for t in page.items] == ["t2"]


def test_metadata_filter_and_range(read_store, seed):
    seed(PROJECT, "old-eu", metadata={"region": "eu"}, timestamp=minutes(0))
    seed(PROJECT, "in-eu", metadata={"region": "eu"}, timestamp=minutes(60))
    seed(PROJECT, "in-us", metadata={"region": "us"}, timestamp=minutes(61))
    seed(PROJECT, "late-eu", metadata={"region": "eu"}, timestamp=minutes(600))
    query = TraceQuery(metadata={"region": "eu"}, from_time=minutes(30), to_time=minutes(120))
    assert [t.id for t in read_store.list_traces(PROJECT, query).items] == ["in-eu"]
