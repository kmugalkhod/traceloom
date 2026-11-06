"""v3.1 layout pass, server side: the "..." menus and the Breakdown tabs render the right controls
for each role. Viewers still see no write controls; nothing a role cannot use is rendered."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers_web import seed_trace
from traceloom.auth.permissions import Role

HX = {"HX-Request": "true"}


@pytest.fixture
def trace(container, world):
    return seed_trace(
        container, world.project.id, "t1", user_id="usr_maria", session_id="sess-1",
        timestamp=datetime.now(UTC) - timedelta(minutes=5),
    )  # fmt: skip


def get(client, login, world, role, path, **kw):
    login(client, world.user(role), csrf=False)
    r = client.get(f"/project/{world.project.id}{path}", **kw)
    assert r.status_code == 200, r.text
    return r.text


def menu(html, menu_id):
    """The whole popup element with this id (balanced on <div>), or None."""
    start = html.find(f'id="{menu_id}" data-menu hidden')
    if start < 0:
        return None
    start = html.rindex("<div", 0, start)
    depth, at = 0, start
    for tag in re.finditer(r"<(/?)div\b", html[start:]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            at = start + tag.end()
            break
    return html[start : html.index(">", at) + 1]


def test_viewer_gets_no_trace_menu_and_no_buttons(client, login, world, trace):
    html = get(client, login, world, Role.VIEWER, "/traces/t1/actions", headers=HX)
    assert "data-menu" not in html and "<button" not in html and "<form" not in html


def test_member_menu_copies_ids_but_cannot_share_or_delete(client, login, world, trace):
    html = get(client, login, world, Role.MEMBER, "/traces/t1/actions", headers=HX)
    items = menu(html, "trace-menu")
    assert items and "Copy trace id" in items and 'data-copy="usr_maria"' in items
    assert "Public share" not in html and "data-dialog-open" not in html and "Delete" not in html
    assert "Bookmark" in html.split('id="trace-menu"')[0]  # the one visible button


def test_admin_menu_holds_share_and_delete(client, login, world, trace):
    html = get(client, login, world, Role.ADMIN, "/traces/t1/actions", headers=HX)
    items = menu(html, "trace-menu")
    assert 'role="switch"' in items and "Public share" in items
    assert 'class="item danger" role="menuitem" data-dialog-open="delete-trace"' in items
    outside = html.replace(items, "")
    assert "Public share" not in outside and "btn-danger btn-sm" not in outside


def test_the_fragment_on_its_own_keeps_every_control_inline(client, login, world, trace):
    """Without HTMX (no JavaScript) there is no menu to open, so nothing is hidden in one."""
    html = get(client, login, world, Role.ADMIN, "/traces/t1/actions")
    assert "data-menu" not in html and 'aria-label="Public share"' in html
    assert 'class="btn btn-danger btn-sm" data-dialog-open="delete-trace"' in html


@pytest.mark.parametrize(
    ("role", "save_view", "delete_matching"),
    [(Role.VIEWER, False, False), (Role.MEMBER, True, False), (Role.ADMIN, True, True)],
)
def test_traces_list_header_has_one_primary_and_a_menu(
    client, login, world, trace, role, save_view, delete_matching
):
    html = get(client, login, world, role, "/traces?tag=x")
    header = html.split('<div class="statrow"')[0].split('<div class="page-h">')[1]
    assert header.count("btn-primary") == int(save_view)
    assert ('data-dialog-open="save-view">' in header) is save_view
    items = header.split('id="list-menu"')[1]
    assert "Saved views" in items and 'name="col"' in items  # columns live in the menu
    assert ("traces-delete-matching?tag=x" in items) is delete_matching
    assert "btn-danger" not in header


def test_dashboard_breakdown_is_one_tabbed_panel(client, login, world, container, trace):
    html = get(client, login, world, Role.VIEWER, "/dashboard")
    tabs = re.findall(r'<button type="button" role="tab"[^>]*>([^<]+)</button>', html)
    assert tabs == ["Cost", "Tokens", "Latency by model", "Latency by trace"]
    assert html.count('aria-selected="true" tabindex="0"') == 1
    assert html.count('aria-selected="false" tabindex="-1"') == 3
    panels = re.findall(
        r'<div class="tabpanel" role="tabpanel" id="(bd-\w+)"[^>]*?( hidden)?>', html
    )
    assert panels == [("bd-cost", ""), ("bd-tokens", " hidden"), ("bd-latm", " hidden"),
                      ("bd-latn", " hidden")]  # fmt: skip
    for tab, panel in (("cost", "bd-cost"), ("latn", "bd-latn")):
        assert f'id="bd-tab-{tab}" aria-controls="{panel}"' in html


@pytest.mark.parametrize("role", [Role.VIEWER, Role.MEMBER])
def test_settings_lists_render_no_row_menus_for_roles_that_cannot_act(client, login, world, role):
    html = get(client, login, world, role, "/settings/members")
    assert "Revoke invite" not in html and ">Remove<" not in html
