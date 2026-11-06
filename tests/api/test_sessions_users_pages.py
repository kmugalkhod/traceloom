"""Sessions list and replay, users list and user page."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers_web import seed_trace
from traceloom.auth.permissions import Role

XSS = "<script>alert(1)</script>"
NOW = datetime.now(UTC)


def pid(world):
    return world.project.id


@pytest.fixture
def conversation(container, world):
    """Five traces of one session, inserted out of order, each with its own cost."""
    spec = [(3, 0.03), (0, 0.01), (4, 0.05), (1, 0.02), (2, 0.04)]
    for minutes, cost in spec:
        seed_trace(
            container, pid(world), f"c{minutes}", name=f"turn {minutes}", session_id="sess-1",
            user_id="alice", tags=["support"], cost=cost, tokens=100 + minutes,
            input=f"question {minutes}", output=f"answer {minutes}",
            timestamp=NOW - timedelta(minutes=30 - minutes * 5),
        )  # fmt: skip


def get(client, login, world, path, role=Role.VIEWER):
    login(client, world.user(role), csrf=False)
    return client.get(f"/project/{pid(world)}{path}")


# --- sessions list ----------------------------------------------------------------------------


def test_sessions_list_shows_the_session_with_its_totals(client, login, world, conversation):
    r = get(client, login, world, "/sessions")
    assert r.status_code == 200
    row = re.search(r"<tr data-row>.*?</tr>", r.text, re.S).group(0)
    assert "sess-1" in row and "alice" in row and "support" in row
    assert re.search(r'<td class="num">5</td>', row)  # five traces
    assert "$0.1500" in row  # 0.01 + 0.02 + 0.03 + 0.04 + 0.05
    assert f"/project/{pid(world)}/sessions/sess-1" in row
    assert "Sessions" in r.text and "Load more" not in r.text


def test_sessions_sort_links_and_order(client, login, world, container):
    for i, (traces, cost) in enumerate([(1, 0.5), (3, 0.1), (2, 0.9)]):
        for j in range(traces):
            when = NOW - timedelta(minutes=i * 10 + j)
            seed_trace(container, pid(world), f"s{i}-{j}", session_id=f"sess-{i}",
                       cost=cost / traces, timestamp=when)  # fmt: skip

    def order(query):
        r = get(client, login, world, "/sessions?" + query)
        return re.findall(r'sessions/(sess-\d)"', r.text)

    assert order("sort=traces&dir=desc") == ["sess-1", "sess-2", "sess-0"]
    assert order("sort=traces&dir=asc") == ["sess-0", "sess-2", "sess-1"]
    assert order("sort=cost") == ["sess-2", "sess-0", "sess-1"]
    assert order("sort=bogus")[0] == "sess-0"  # falls back to the newest first
    html = get(client, login, world, "/sessions?sort=cost").text
    assert f'href="/project/{pid(world)}/sessions?sort=cost&amp;dir=asc"' in html


def test_sessions_cursor_pagination_has_no_gaps_or_repeats(client, login, world, container):
    for i in range(55):
        seed_trace(container, pid(world), f"p{i:02d}", session_id=f"sess-{i:02d}",
                   timestamp=NOW - timedelta(minutes=i))  # fmt: skip
    first = get(client, login, world, "/sessions")
    ids = re.findall(r'sessions/(sess-\d+)"', first.text)
    assert len(ids) == 50
    nxt = re.search(r'href="([^"]+cursor=[^"]+)"', first.text).group(1).replace("&amp;", "&")
    second = client.get(nxt)
    ids2 = re.findall(r'sessions/(sess-\d+)"', second.text)
    assert len(ids2) == 5 and not set(ids) & set(ids2)
    assert sorted(ids + ids2) == [f"sess-{i:02d}" for i in range(55)]
    assert "Load more" not in second.text and "First page" in second.text
    assert client.get(f"/project/{pid(world)}/sessions?cursor=junk").status_code == 400


def test_sessions_date_range_and_environment_filters(client, login, world, container):
    seed_trace(
        container,
        pid(world),
        "new",
        session_id="sess-new",
        environment="prod",
        timestamp=NOW - timedelta(hours=2),
    )
    seed_trace(
        container,
        pid(world),
        "old",
        session_id="sess-old",
        environment="prod",
        timestamp=NOW - timedelta(days=3),
    )
    seed_trace(container, pid(world), "dev", session_id="sess-dev", environment="dev",
               timestamp=NOW - timedelta(hours=1))  # fmt: skip

    def ids(query):
        return sorted(
            re.findall(r'sessions/(sess-\w+)"', get(client, login, world, "/sessions" + query).text)
        )

    assert ids("") == ["sess-dev", "sess-new"]  # default: last 24 hours
    assert ids("?range=7d") == ["sess-dev", "sess-new", "sess-old"]
    assert ids("?env=prod&range=7d") == ["sess-new", "sess-old"]
    html = get(client, login, world, "/sessions?env=prod").text
    assert "env=prod&amp;range=7d" in html  # the range links keep the environment
    assert 'aria-pressed="true">24h' in html


def test_empty_sessions_page_explains_how_to_get_sessions(client, login, world):
    r = get(client, login, world, "/sessions")
    assert r.status_code == 200 and "No sessions yet" in r.text and "session_id" in r.text


# --- replay ---------------------------------------------------------------------------------------


def test_replay_lists_turns_in_time_order_with_totals(client, login, world, conversation):
    r = get(client, login, world, "/sessions/sess-1")
    assert r.status_code == 200
    questions = re.findall(r"question (\d)", r.text)
    assert questions == ["0", "1", "2", "3", "4"]  # inserted 3,0,4,1,2
    costs = re.findall(r'<span class="mono">(\$0\.\d{4})</span>', r.text)
    assert costs[:5] == ["$0.0100", "$0.0200", "$0.0400", "$0.0300", "$0.0500"]
    assert "$0.1500" in r.text and ">5</b>" in r.text  # totals on top equal the sums
    assert "$0.0100 + $0.0200 + $0.0400 + $0.0300 + $0.0500" in r.text
    assert f"/project/{pid(world)}/traces/c0" in r.text
    assert "alice" in r.text


def test_replay_renders_script_text_literally(client, login, world, container):
    seed_trace(container, pid(world), "x1", session_id="sx", input=XSS, output=f"reply {XSS}")
    chat = [{"role": "user", "content": XSS}]
    seed_trace(container, pid(world), "x2", session_id="sx", input=chat,
                output={"k": XSS}, name=XSS, timestamp=NOW + timedelta(seconds=1))  # fmt: skip
    r = get(client, login, world, "/sessions/sx")
    assert "<script>alert" not in r.text
    assert r.text.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 4


def test_replay_marks_failed_turns(client, login, world, container):
    seed_trace(container, pid(world), "bad", session_id="se", output="oops")
    from traceloom.ports import ObservationRow

    store = container.trace_store
    store.upsert(
        pid(world),
        [],
        [ObservationRow(id="bad-err", trace_id="bad", start_time=NOW, level="ERROR", event_ts=NOW)],
        [],
    )
    store.refresh_trace_aggregates(pid(world), ["bad"])
    r = get(client, login, world, "/sessions/se")
    assert "step failed" in r.text and 'class="bub e"' in r.text


def test_replay_404s(client, login, world, conversation, make_project, container):
    other = make_project(world.org, "Other")
    seed_trace(container, other.id, "o1", session_id="only-other")
    assert get(client, login, world, "/sessions/ghost").status_code == 404
    assert get(client, login, world, "/sessions/only-other").status_code == 404  # another project's
    assert client.get(f"/project/{other.id}/sessions/sess-1").status_code == 404  # not found there


def test_session_ids_with_special_characters(client, login, world, container):
    seed_trace(container, pid(world), "w1", session_id="a/b c?d", timestamp=NOW)
    listing = get(client, login, world, "/sessions").text
    href = re.search(r'href="(/project/[^"]+/sessions/[^"]+)"', listing).group(1)
    assert "a/b%20c%3Fd" in href
    assert client.get(href).status_code == 200


def test_pages_need_a_login_and_membership(
    client, login, world, conversation, make_org, make_project, make_user, make_membership
):
    r = client.get(f"/project/{pid(world)}/sessions", follow_redirects=False)
    assert r.status_code in (303, 401)
    outsider = make_user(email="out@example.com")
    foreign = make_project(make_org("Foreign"))
    login(client, outsider, csrf=False)
    assert client.get(f"/project/{pid(world)}/sessions").status_code == 404
    assert client.get(f"/project/{pid(world)}/users").status_code == 404
    assert client.get(f"/project/{foreign.id}/sessions").status_code == 404  # not a member there


# --- users --------------------------------------------------------------------------------


@pytest.fixture
def people(container, world):
    seed_trace(container, pid(world), "u1", user_id="alice", cost=0.25, tokens=1000, name="chat",
               timestamp=NOW - timedelta(hours=3))  # fmt: skip
    seed_trace(container, pid(world), "u2", user_id="alice", cost=0.75, tokens=2000, name="chat",
               timestamp=NOW - timedelta(hours=1))  # fmt: skip
    seed_trace(container, pid(world), "u3", user_id="bob", cost=0.10, tokens=300, name="search",
               timestamp=NOW - timedelta(hours=2), session_id="bob-sess")  # fmt: skip


def test_users_list_counts_tokens_and_cost(client, login, world, people):
    r = get(client, login, world, "/users")
    assert r.status_code == 200
    rows = {
        m.group(1): m.group(0)
        for m in re.finditer(r'<tr data-row>.*?users/(\w+)".*?</tr>', r.text, re.S)
    }
    assert set(rows) == {"alice", "bob"}
    assert (
        re.search(r'<td class="num">2</td>', rows["alice"])
        and "3,000" in rows["alice"]
        and "$1.0000" in rows["alice"]
    )
    assert (
        re.search(r'<td class="num">1</td>', rows["bob"])
        and "300" in rows["bob"]
        and "$0.1000" in rows["bob"]
    )
    order = re.findall(r'users/(\w+)"', get(client, login, world, "/users?sort=cost").text)
    assert order == ["alice", "bob"]
    order = re.findall(r'users/(\w+)"', get(client, login, world, "/users?sort=cost&dir=asc").text)
    assert order == ["bob", "alice"]


def test_user_page_shows_usage_and_links_to_filtered_traces(client, login, world, people):
    r = get(client, login, world, "/users/alice")
    assert r.status_code == 200
    assert "alice" in r.text and "$1.0000" in r.text and "3,000" in r.text
    assert f'href="/project/{pid(world)}/traces?user=alice"' in r.text
    assert (
        f"/project/{pid(world)}/traces/u1" in r.text
        and f"/project/{pid(world)}/traces/u2" in r.text
    )
    assert "u3" not in r.text  # another user's trace
    assert re.search(r"<td class=\"mono\">\d{4}-\d\d-\d\d</td>", r.text)  # the per-day table
    bob = get(client, login, world, "/users/bob").text
    assert f"/project/{pid(world)}/sessions/bob-sess" in bob


def test_user_page_404_and_isolation(client, login, world, people, make_project, container):
    other = make_project(world.org, "Other")
    seed_trace(container, other.id, "z1", user_id="zed")
    assert get(client, login, world, "/users/zed").status_code == 404
    assert get(client, login, world, "/users/nobody").status_code == 404
    assert client.get(f"/project/{other.id}/users/alice").status_code == 404


def test_user_ids_with_markup_or_slashes_are_safe(client, login, world, container):
    seed_trace(container, pid(world), "m1", user_id=f"{XSS}/x y")
    listing = get(client, login, world, "/users").text
    assert "<script>alert" not in listing and "&lt;script&gt;" in listing
    href = re.search(r'href="(/project/[^"]+/users/[^"]+)"', listing).group(1)
    page = client.get(href)
    assert page.status_code == 200 and "<script>alert" not in page.text


def test_empty_users_page(client, login, world):
    r = get(client, login, world, "/users")
    assert r.status_code == 200 and "No users yet" in r.text


def test_data_pages_are_csp_clean(client, login, world, conversation, people):
    for path in ("/sessions", "/sessions/sess-1", "/users", "/users/alice"):
        html = get(client, login, world, path).text
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
        assert not re.search(r"\sstyle=", html) and not re.search(r"\son\w+=", html)


# --- environment: the saved choice, overridden by ?env= ------------------------------------------


def test_saved_environment_applies_to_sessions_users_and_dashboard(
    client, login, world, container, db
):
    from traceloom.db.models import UserPref

    for env in ("prod", "dev"):
        seed_trace(
            container, pid(world), f"t-{env}", session_id=f"sess-{env}", user_id=f"user-{env}",
            environment=env, cost=1.0, timestamp=NOW,
        )  # fmt: skip
    login(client, world.user(Role.VIEWER), csrf=False)
    db.add(
        UserPref(user_id=world.user(Role.VIEWER).id, project_id=pid(world), key="env", value="prod")
    )
    db.commit()
    base = f"/project/{pid(world)}"

    def seen(path, pattern):
        return sorted(set(re.findall(pattern, client.get(base + path).text)))

    for path, pattern in (
        ("/sessions", r"sessions/(sess-\w+)\""),
        ("/users", r"users/(user-\w+)\""),
    ):
        assert seen(path, pattern) == [pattern.split("(")[1].split("-")[0] + "-prod"], path
        assert (
            len(seen(path + "?env=dev", pattern)) == 1
            and "dev" in seen(path + "?env=dev", pattern)[0]
        )
        assert len(seen(path + "?env=all", pattern)) == 2
    assert (
        'class="stat"><div class="top">Traces</div><b>1</b>' in client.get(base + "/dashboard").text
    )
    assert "<b>2</b>" in client.get(base + "/dashboard?env=all").text


def test_env_resolution_matches_the_list():
    from traceloom.web.envpref import resolve_env

    assert resolve_env({}, "prod") == "prod" and resolve_env({"env": "all"}, "prod") is None
    assert resolve_env({"env": "dev"}, "prod") == "dev" and resolve_env({}, None) is None


def test_pages_show_the_shared_environment_switcher(client, login, world, container, db):
    seed_trace(
        container, pid(world), "e1", environment="prod", session_id="s", user_id="u", timestamp=NOW
    )
    login(client, world.user(Role.VIEWER), csrf=False)
    for path in ("/sessions", "/users", "/dashboard"):
        html = client.get(f"/project/{pid(world)}{path}?env=prod").text
        assert f'action="/project/{pid(world)}/prefs/env"' in html and 'value="prod"' in html, path
