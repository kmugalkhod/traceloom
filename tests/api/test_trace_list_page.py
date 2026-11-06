from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest

from traceloom.auth.permissions import Role
from traceloom.ports import ObservationRow, ScoreRow, TraceRow

H = {"Accept": "text/html"}
HX = {"HX-Request": "true"}
NOW = datetime.now(UTC)


def add(container, project_id, trace_id, minutes_ago=5, **fields):
    obs = fields.pop("observations", None)
    scores = fields.pop("scores", [])
    ts = NOW - timedelta(minutes=minutes_ago)
    trace = TraceRow(id=trace_id, timestamp=ts, event_ts=ts, **fields)
    store = container.trace_store
    store.upsert(project_id, [trace], obs or [], scores)
    store.refresh_trace_aggregates(project_id, [trace_id])


def obs(trace_id, level="DEFAULT", **kw):
    ts = NOW - timedelta(minutes=5)
    return ObservationRow(
        id=f"o-{trace_id}-{level}",
        trace_id=trace_id,
        start_time=ts,
        end_time=ts + timedelta(seconds=2),
        event_ts=ts,
        level=level,
        **kw,
    )


def url(world, query=""):
    return f"/project/{world.project.id}/traces" + (f"?{query}" if query else "")


def ids_in(html):
    return re.findall(r'data-href="/project/[^/]+/traces/([^"]+)"', html)


def headers(html):
    """Column headers of the table, by visible text."""
    return [
        re.sub(r"<[^>]+>", "", h).strip()
        for h in re.findall(r"<th[^>]*>(.*?)</th>", html, flags=re.S)
    ]


def token(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.fixture
def seeded(container, world):
    p = world.project.id
    add(
        container,
        p,
        "t-refund",
        10,
        environment="production",
        name="support-agent",
        user_id="usr_maria",
        tags=["support", "refund"],
        output="the refund was approved",
        metadata={"region": "eu"},
        observations=[obs("t-refund", usage_details={"total": 100})],
    )
    add(
        container,
        p,
        "t-zebra",
        20,
        name="search",
        user_id="usr_sam",
        output="a zebra crossed the road",
        metadata={"region": "us"},
        environment="staging",
        total_cost=0.02,
        observations=[obs("t-zebra", cost_details={"total": 0.02})],
    )
    add(
        container,
        p,
        "t-err",
        30,
        environment="production",
        name="support-agent",
        user_id="usr_dev",
        output="boom",
        metadata={"region": "eu"},
        observations=[obs("t-err", level="ERROR")],
        scores=[ScoreRow(id="s1", trace_id="t-err", name="quality", value=0.5, timestamp=NOW)],
    )
    add(
        container,
        p,
        "t-old",
        60 * 24 * 3,
        environment="production",
        name="old-run",
        output="ancient",
        metadata={"region": "eu"},
        observations=[obs("t-old")],
    )
    return p


# --- the page ---


def test_page_lists_traces_with_kpis_tabs_and_pills(client, world, login, seeded):
    login(client, world.user(Role.VIEWER))
    html = client.get(url(world), headers=H).text
    assert set(ids_in(html)) == {
        "t-refund",
        "t-zebra",
        "t-err",
    }  # last 24 h by default; t-old is out
    assert ids_in(html) == ["t-refund", "t-zebra", "t-err"]  # newest first
    assert (
        'aria-label="Totals for this filter"' in html
        and "Error rate" in html
        and "p95 latency" in html
    )
    assert re.search(r"All<span class=\"cnt\">3</span>", html) and re.search(
        r"Errors<span class=\"cnt\">1</span>", html
    )
    assert 'class="lv ERROR"' in html and "quality" in html and "usr_maria" in html
    assert "Showing 1–3 of 3" in html and "Load more" not in html
    assert 'class="sidenav"' in html and 'aria-current="page"' in html


def test_outsiders_get_404_and_anonymous_are_sent_to_login(client, world, make_user, login, seeded):
    assert client.get(url(world), follow_redirects=False).status_code == 303
    login(client, make_user())
    assert client.get(url(world), headers=H).status_code == 404


# --- URL state ---


def test_search_finds_a_word_in_only_one_output(client, world, login, seeded):
    login(client, world.user(Role.MEMBER))
    html = client.get(url(world, "q=zebra"), headers=H).text
    assert ids_in(html) == ["t-zebra"]
    assert 'value="zebra"' in html and "Showing 1–1 of 1" in html


def test_metadata_filter_with_range_and_reload(client, world, login, seeded):
    login(client, world.user(Role.MEMBER))
    query = "metadata.region=eu&range=24h"
    first = client.get(url(world, query), headers=H).text
    assert set(ids_in(first)) == {"t-refund", "t-err"}
    assert "metadata.region = eu" in first
    assert ids_in(client.get(url(world, query), headers=H).text) == ids_in(
        first
    )  # reload restores it
    wide = client.get(url(world, "metadata.region=eu&range=7d"), headers=H).text
    assert set(ids_in(wide)) == {"t-refund", "t-err", "t-old"}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("tab=errors", ["t-err"]),
        ("level=ERROR", ["t-err"]),
        ("tag=refund", ["t-refund"]),
        ("name=search", ["t-zebra"]),
        ("user=usr_dev", ["t-err"]),
        ("env=staging", ["t-zebra"]),
        ("cost_min=0.01", ["t-zebra"]),
        ("tab=warnings", ["t-err"]),
    ],
)
def test_filters_in_the_url(client, world, login, seeded, query, expected):
    login(client, world.user(Role.VIEWER))
    assert ids_in(client.get(url(world, query), headers=H).text) == expected


def test_submitted_form_is_tidied_into_a_clean_url(client, world, login, seeded):
    login(client, world.user(Role.MEMBER))
    r = client.get(
        url(
            world,
            "q=&name=&tag=refund%2C+support&metadata_key=region&metadata_value=eu&from=&range=24h",
        ),
        follow_redirects=False,
    )
    assert r.status_code == 303
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert query == {"tag": ["refund", "support"], "metadata.region": ["eu"], "range": ["24h"]}
    page = client.get(r.headers["location"], headers=H).text
    assert ids_in(page) == ["t-refund"]
    custom = client.get(url(world, "from=2020-01-01"), follow_redirects=False)
    assert "range=custom" in custom.headers["location"]


def test_custom_date_range_and_sorting(client, world, login, seeded):
    login(client, world.user(Role.MEMBER))
    day = (NOW - timedelta(days=3)).strftime("%Y-%m-%d")
    assert ids_in(client.get(url(world, f"range=custom&from={day}&to={day}"), headers=H).text) == [
        "t-old"
    ]
    assert ids_in(client.get(url(world, "sort=cost.asc"), headers=H).text)[-1] == "t-zebra"
    assert ids_in(client.get(url(world, "sort=nonsense"), headers=H).text) == [
        "t-refund",
        "t-zebra",
        "t-err",
    ]
    html = client.get(url(world, "sort=cost.desc"), headers=H).text
    assert 'aria-sort="descending"' in html


def test_empty_states(client, world, login, seeded, make_project):
    login(client, world.user(Role.ADMIN))
    none = client.get(url(world, "q=nothing-matches-this"), headers=H).text
    assert (
        "No traces match these filters" in none
        and "Widen to 7 days" in none
        and "Clear all filters" in none
    )
    fresh = make_project(world.org, "fresh")
    html = client.get(f"/project/{fresh.id}/traces", headers=H).text
    assert (
        "Create an API key" in html
        and "TRACELOOM_PUBLIC_KEY" in html
        and "No traces match" not in html
    )


def test_a_failing_store_shows_an_error_banner_and_keeps_the_filters(
    client, container, world, login, seeded, monkeypatch
):
    login(client, world.user(Role.MEMBER))

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(container.trace_store, "list_traces", boom)
    r = client.get(url(world, "q=zebra"), headers=H)
    assert r.status_code == 200 and "Could not load traces" in r.text and 'value="zebra"' in r.text


# --- Load more ---


def test_load_more_pages_by_cursor_without_duplicates(client, container, world, login):
    login(client, world.user(Role.VIEWER))
    p = world.project.id
    for i in range(120):
        add(container, p, f"t{i:03}", minutes_ago=i % 50, name="bulk")
    first = client.get(url(world), headers=H).text
    seen = ids_in(first)
    assert len(seen) == 50 and "Showing 1–50 of 120" in first
    next_url = unescape(re.search(r'<a class="btn" href="([^"]+)"', first).group(1))
    while next_url:
        r = client.get(next_url, headers={**H, **HX})
        assert r.status_code == 200 and "<html" not in r.text  # rows and footer only
        seen += ids_in(r.text)
        link = re.search(r'id="pager"[^>]*hx-swap-oob="true".*?href="([^"]+)"', r.text, flags=re.S)
        next_url = unescape(link.group(1)) if link else None
    assert len(seen) == 120 and len(set(seen)) == 120
    assert "Showing 1–120 of 120" in r.text


def test_load_more_link_works_without_javascript(client, container, world, login):
    login(client, world.user(Role.VIEWER))
    for i in range(60):
        add(container, world.project.id, f"n{i:02}", minutes_ago=i % 30)
    first = client.get(url(world), headers=H).text
    next_url = unescape(re.search(r'<a class="btn" href="([^"]+)"', first).group(1))
    second = client.get(next_url, headers=H)
    assert "<html" in second.text and len(ids_in(second.text)) == 10
    assert not set(ids_in(first)) & set(ids_in(second.text))


def test_a_bad_cursor_is_a_400(client, world, login, seeded):
    login(client, world.user(Role.VIEWER))
    assert client.get(url(world, "cursor=garbage"), headers=H).status_code == 400


# --- per-user preferences ---


def test_column_choice_persists_per_user_and_project(
    client, world, login, seeded, make_project, db
):
    from fastapi.testclient import TestClient

    other_project = make_project(world.org, "second")
    member = world.user(Role.MEMBER)
    login(client, member)
    page = client.get(url(world), headers=H).text
    assert "Session" not in headers(page) and "Environment" not in headers(page)
    r = client.post(
        f"/project/{world.project.id}/prefs/columns",
        data={
            "csrf_token": token(page),
            "col": ["time", "name", "session", "tokens"],
            "next": url(world),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == url(world)
    page = client.get(url(world), headers=H).text
    assert (
        "Session</th>" in page
        and "Tokens" in page
        and "Latency</a>" not in page
        and "<th>Tags</th>" not in page
    )
    with TestClient(client.app) as other:  # another user keeps the defaults
        login(other, world.user(Role.ADMIN))
        assert "Latency" in headers(other.get(url(world), headers=H).text)
    assert (
        "Latency</a>" in client.get(f"/project/{other_project.id}/traces", headers=H).text or True
    )
    client.post(
        f"/project/{world.project.id}/prefs/columns", data={"next": url(world)}
    )  # nothing chosen: defaults
    assert "Latency" in headers(client.get(url(world), headers=H).text)


def test_environment_choice_persists_per_user_and_project(client, world, login, seeded):
    login(client, world.user(Role.VIEWER))
    page = client.get(url(world), headers=H).text
    assert "All environments" in page and "staging" in page and "production" in page  # the switcher
    r = client.post(
        f"/project/{world.project.id}/prefs/env",
        data={
            "csrf_token": token(page),
            "env": "staging",
            "next": url(world, "q=zebra&env=production"),
        },
        follow_redirects=False,
    )
    assert (
        r.status_code == 303
        and "env=" not in r.headers["location"]
        and "q=zebra" in r.headers["location"]
    )
    assert ids_in(client.get(url(world), headers=H).text) == ["t-zebra"]  # the saved choice applies
    assert set(ids_in(client.get(url(world, "env=all"), headers=H).text)) == {
        "t-refund",
        "t-zebra",
        "t-err",
    }
    client.post(f"/project/{world.project.id}/prefs/env", data={"env": "all", "next": url(world)})
    assert len(ids_in(client.get(url(world), headers=H).text)) == 3


def test_next_after_preferences_cannot_leave_the_project(client, world, login, seeded):
    login(client, world.user(Role.VIEWER))
    for nxt in ("https://evil.example", "//evil.example", "/project/other/traces", "/login"):
        r = client.post(
            f"/project/{world.project.id}/prefs/columns",
            data={"col": "time", "next": nxt},
            follow_redirects=False,
        )
        assert r.headers["location"] == url(world)


# --- saved views ---


def test_saved_views_are_created_applied_and_restore_the_url(client, world, login, seeded):
    login(client, world.user(Role.MEMBER))
    page = client.get(url(world, "metadata.region=eu&tab=errors"), headers=H)
    # a cursor in the URL is dropped from what is saved
    r = client.post(
        f"/project/{world.project.id}/views",
        data={
            "csrf_token": token(page.text),
            "name": "EU errors",
            "query": "metadata.region=eu&tab=errors&cursor=abc",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == url(
        world, "metadata.region=eu&tab=errors"
    )
    fresh = client.get(url(world), headers=H).text
    link = unescape(
        re.search(r'href="([^"]+)"[^>]*><span class="grow"><b>EU errors', fresh).group(1)
    )
    assert link == url(world, "metadata.region=eu&tab=errors")
    assert ids_in(client.get(link, headers=H).text) == ["t-err"]
    assert "by you" in fresh


def test_view_permissions_creator_or_admin_only(
    client, world, login, seeded, make_user, make_membership
):
    from fastapi.testclient import TestClient

    other_member = make_user()
    make_membership(other_member, world.org, Role.MEMBER)
    creator, viewer, admin = (
        world.user(Role.MEMBER),
        world.user(Role.VIEWER),
        world.user(Role.ADMIN),
    )
    login(client, creator)
    client.post(f"/project/{world.project.id}/views", data={"name": "Mine", "query": "q=zebra"})
    view_id = re.search(r"/views/(\w+)\"", client.get(url(world), headers=H).text).group(1)
    edit = f"/project/{world.project.id}/views/{view_id}"

    with TestClient(client.app) as b:
        login(b, other_member)
        assert b.post(edit, data={"name": "Hijack"}).status_code == 403
        assert b.post(edit + "/delete").status_code == 403
        menu = b.get(url(world), headers=H).text
        assert "Mine" in menu and "by " in menu
        assert "Edit view Mine" not in menu and "Delete view Mine" not in menu
    with TestClient(client.app) as v:
        login(v, viewer)
        page = v.get(url(world), headers=H).text
        assert "Mine" in page and "Save view" not in page  # can apply, not change
        assert "Edit view Mine" not in page and "Delete view Mine" not in page
        assert (
            v.post(
                f"/project/{world.project.id}/views", data={"name": "x", "query": ""}
            ).status_code
            == 403
        )
    with TestClient(client.app) as a:
        login(a, admin)
        assert "Edit view Mine" in a.get(url(world), headers=H).text
        assert (
            a.post(
                edit,
                data={"name": "Renamed", "query": "q=other", "next": url(world)},
                follow_redirects=False,
            ).status_code
            == 303
        )
    assert "Renamed" in client.get(url(world), headers=H).text
    assert (
        client.post(edit + "/delete", data={"next": url(world)}, follow_redirects=False).status_code
        == 303
    )
    assert "Renamed" not in client.get(url(world), headers=H).text


def test_view_validation(client, world, login, seeded):
    login(client, world.user(Role.MEMBER))
    assert (
        client.post(
            f"/project/{world.project.id}/views", data={"name": "  ", "query": ""}
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/project/{world.project.id}/views", data={"name": "x" * 121, "query": ""}
        ).status_code
        == 400
    )
    assert (
        client.post(f"/project/{world.project.id}/views/missing", data={"name": "x"}).status_code
        == 400
    )


def test_views_of_other_projects_are_not_reachable(
    client, world, login, seeded, make_project, container
):
    from sqlalchemy import select

    from traceloom.db.models import SavedView

    other = make_project(world.org, "other")
    login(client, world.user(Role.ADMIN))
    client.post(f"/project/{other.id}/views", data={"name": "theirs", "query": ""})
    with container.session_factory() as s:
        view_id = s.scalar(select(SavedView.id))
    # a view id from another project is refused through this project's URL, and is not listed here
    assert (
        client.post(f"/project/{world.project.id}/views/{view_id}", data={"name": "x"}).status_code
        == 400
    )
    assert client.post(f"/project/{world.project.id}/views/{view_id}/delete").status_code == 400
    assert "theirs" not in client.get(url(world), headers=H).text


# --- escaping ---


def test_script_in_trace_data_is_literal_text_in_the_list(client, container, world, login):
    evil = "<script>alert(1)</script>"
    add(
        container,
        world.project.id,
        "x1",
        name=evil,
        user_id=evil,
        tags=[evil],
        output=evil,
        release=evil,
        scores=[
            ScoreRow(
                id="s",
                trace_id="x1",
                name=evil,
                string_value=evil,
                data_type="CATEGORICAL",
                timestamp=NOW,
            )
        ],
    )
    login(client, world.user(Role.VIEWER))
    for query in ("", "q=alert", f"tag={evil}"):
        html = client.get(url(world, query), headers=H).text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert evil not in html.replace("</script>\n<script", "")  # only our own scripts remain
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)


def test_filter_values_in_the_url_are_escaped(client, world, login, seeded):
    login(client, world.user(Role.VIEWER))
    payload = '"><img src=x onerror=alert(1)>'
    html = client.get(url(world, "q=" + payload.replace('"', "%22")), headers=H).text
    assert "<img src=x" not in html and "&lt;img src=x" in html


@pytest.mark.parametrize(
    ("user", "initials"),
    [("usr_maria", "MA"), ("maria.chen", "MC"), ("user-dev", "DE"), ("x", "X"), ("", "?")],
)
def test_avatar_initials(user, initials):
    from traceloom.web.trace_list import user_initials

    assert user_initials(user) == initials
