"""QA: the spec documents the GET filters in snake_case (spec.md: ?trace_id=&config_id=)."""

from __future__ import annotations

import pytest

from tests.api.test_public_scores import URL, auth, post, trace  # noqa: F401


@pytest.mark.parametrize("param", ["trace_id", "traceId"])
def test_trace_filter_is_honoured_in_both_spellings(client, auth, param):  # noqa: F811
    post(client, auth, traceId="t1", name="a", value=1)
    got = client.get(URL, params={param: "no-such-trace"}, headers=auth).json()["data"]
    assert got == [], f"?{param}= was ignored and returned every score"


def test_unknown_filter_values_never_widen_the_result(client, auth):  # noqa: F811
    post(client, auth, traceId="t1", name="a", value=1)
    for params in ({"config_id": "nope"}, {"configId": "nope"}, {"source": "EVAL"}):
        assert client.get(URL, params=params, headers=auth).json()["data"] == []
