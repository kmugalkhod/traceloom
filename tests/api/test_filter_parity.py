"""Delete-matching must remove exactly the traces the list shows for the same URL.

The list parses its URL with `trace_list.parse_state`; delete-matching has its own parser
(`trace_filters`). If they ever disagree, deleting would remove more or fewer traces than the user
saw. The first tests pin each filter on its own; the last compares the two parsers directly.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest
from starlette.datastructures import QueryParams

from tests.helpers_web import form_token, seed_trace
from traceloom.auth.permissions import Role
from traceloom.ports import TraceQuery
from traceloom.web import trace_filters

NOW = datetime.now(UTC)
BASE = dict(
    name="agent", user_id="u1", session_id="s1", tags=["red", "blue"], environment="prod",
    release="r1", version="v1", metadata={"region": "eu"}, bookmarked=True, cost=1.0,
    level="ERROR", duration_ms=2000, input="needle in the input", timestamp=NOW,
)  # fmt: skip

# every filter the list understands, with the value that selects BASE
URL_FILTERS = [
    ("q", "needle"),
    ("name", "agent"),
    ("user", "u1"),
    ("session", "s1"),
    ("tag", "red"),
    ("level", "ERROR"),
    ("has_error", "1"),
    ("bookmarked", "1"),
    ("env", "prod"),
    ("release", "r1"),
    ("version", "v1"),
    ("metadata.region", "eu"),
    ("lat_min", "1"),
    ("lat_max", "3"),
    ("cost_min", "0.5"),
    ("cost_max", "2"),
    ("range", "1h"),
]
# one decoy per filter: identical to BASE except for what that filter looks at
DECOYS = {
    "q": dict(input="haystack"),
    "name": dict(name="other"),
    "user": dict(user_id="u2"),
    "session": dict(session_id="s2"),
    "tag": dict(tags=["green"]),
    "level": dict(level="DEFAULT"),
    "has_error": dict(level="WARNING"),
    "bookmarked": dict(bookmarked=False),
    "env": dict(environment="dev"),
    "release": dict(release="r2"),
    "version": dict(version="v2"),
    "metadata.region": dict(metadata={"region": "us"}),
    "lat_min": dict(duration_ms=100),
    "lat_max": dict(duration_ms=9000),
    "cost_min": dict(cost=0.1),
    "cost_max": dict(cost=5.0),
    "range": dict(timestamp=NOW - timedelta(hours=3)),
}


def pid(world):
    return world.project.id


@pytest.fixture
def traces(container, world):
    seed_trace(container, pid(world), "match", **BASE)
    for key, change in DECOYS.items():
        seed_trace(container, pid(world), f"decoy-{key}", **{**BASE, **change})
    return [f"decoy-{k}" for k in DECOYS]


def admin(client, login, world):
    login(client, world.user(Role.ADMIN), csrf=False)


def dialog_count(client, world, query):
    r = client.get(f"/project/{pid(world)}/traces-delete-matching?{query}")
    assert r.status_code == 200
    return int(re.search(r"Delete (\d+) trace", r.text).group(1)), r.text


def test_all_filters_together_select_exactly_the_match(client, login, world, traces):
    admin(client, login, world)
    count, _ = dialog_count(client, world, urlencode(URL_FILTERS))
    assert count == 1


@pytest.mark.parametrize("key", [k for k, _ in URL_FILTERS])
def test_each_filter_excludes_its_decoy(client, login, world, traces, container, key):
    admin(client, login, world)
    value = dict(URL_FILTERS)[key]
    with_filter = container.trace_store.matching_trace_ids
    count, _ = dialog_count(client, world, urlencode([(key, value)]))
    expected = {t for t in ["match", *traces] if t != f"decoy-{key}"}
    # each filter alone matches everything that satisfies it: all but its own decoy, plus any
    # decoys that happen to satisfy it too (none of the others change this one's attribute)
    if key == "has_error":
        expected -= {"decoy-level"}  # a DEFAULT-level decoy has no error either
    if key == "level":
        expected -= {"decoy-has_error"}  # WARNING is below the ERROR minimum
    assert count == len(expected), (key, count, sorted(expected))
    assert with_filter  # the store method exists on the container's store


SPECS = {"match": BASE, **{f"decoy-{k}": {**BASE, **change} for k, change in DECOYS.items()}}


def expect(predicate):
    return sum(1 for spec in SPECS.values() if predicate(spec))


def test_tab_and_default_range_and_sort_params(client, login, world, traces):
    admin(client, login, world)
    errors = dialog_count(client, world, "tab=errors&sort=cost.asc&cursor=x&shown=50")[0]
    assert errors == expect(lambda s: s["level"] == "ERROR")
    assert dialog_count(client, world, "tab=bookmarked")[0] == expect(lambda s: s["bookmarked"])
    warnings = dialog_count(client, world, "tab=warnings")[0]
    assert warnings == expect(lambda s: s["level"] in ("WARNING", "ERROR"))
    assert dialog_count(client, world, "")[0] == len(SPECS)  # everything is inside 24 hours
    assert dialog_count(client, world, "range=1h")[0] == expect(lambda s: s["timestamp"] == NOW)
    assert dialog_count(client, world, "range=7d")[0] == len(SPECS)


def test_saved_environment_applies_like_it_does_on_the_list(client, login, world, traces, db):
    from traceloom.db.models import UserPref

    admin(client, login, world)
    pref = UserPref(
        user_id=world.user(Role.ADMIN).id, project_id=pid(world), key="env", value="prod"
    )
    db.add(pref)
    db.commit()
    count, html = dialog_count(client, world, "")
    assert count == expect(lambda s: s["environment"] == "prod") and "env: prod" in html
    assert dialog_count(client, world, "env=all")[0] == len(SPECS)
    assert dialog_count(client, world, "env=dev")[0] == 1
    # the dialog bakes the environment in, so later preference changes cannot widen the delete
    assert 'name="filter" value="range=24h&amp;env=prod"' in html


def test_delete_removes_exactly_the_counted_traces(client, login, world, traces, container):
    admin(client, login, world)
    query = urlencode([("tag", "red"), ("env", "prod"), ("level", "ERROR")])
    count, html = dialog_count(client, world, query)
    expected = set(
        container.trace_store.matching_trace_ids(
            pid(world), trace_filters.parse_trace_query(QueryParams(query))
        )
    )
    assert count == len(expected) and "match" in expected
    before = set(container.trace_store.matching_trace_ids(pid(world), TraceQuery()))
    action = f"/project/{pid(world)}/traces-delete-matching"
    token = form_token(html, action)
    sent_filter = re.search(r'name="filter" value="([^"]*)"', html).group(1).replace("&amp;", "&")
    r = client.post(
        action,
        data={"csrf_token": token, "filter": sent_filter, "confirm": str(count)},
        follow_redirects=False,
    )  # fmt: skip
    assert r.status_code == 303
    after = set(container.trace_store.matching_trace_ids(pid(world), TraceQuery()))
    assert before - after == expected


def test_parity_with_the_traces_list_parser():
    trace_list = pytest.importorskip("traceloom.web.trace_list")
    now = datetime.now(UTC)
    urls = [
        urlencode(URL_FILTERS),
        "tab=errors", "tab=warnings&level=ERROR", "tab=bookmarked&tag=a&tag=b",
        "range=custom&from=2026-11-01&to=2026-11-03", "range=custom", "range=bogus&env=all",
        "lat_min=0.5&cost_max=3&metadata.a=1&metadata.b=2&has_error=0", "q=%20spaced%20",
    ]  # fmt: skip
    for pref in (None, "prod"):
        for url in urls:
            params = QueryParams(url)
            theirs = trace_list.parse_state(params, "/x", pref, now=now).query
            ours = trace_filters.parse_trace_query(params, pref, now=now)
            for field in ("q", "name", "user_id", "session_id", "tags", "min_level", "environment",
                          "release", "version", "has_error", "metadata", "latency_min_ms",
                          "latency_max_ms", "cost_min", "cost_max", "bookmarked", "from_time",
                          "to_time"):  # fmt: skip
                assert getattr(ours, field) == getattr(theirs, field), (url, pref, field)
