"""App shell: static assets under the CSP, 403/404 inside the shell, no inline code."""

from __future__ import annotations

import re

import pytest
from fastapi import APIRouter, Depends

from traceloom.auth.deps import Access, require
from traceloom.auth.permissions import Perm, Role
from traceloom.main import create_app

probe = APIRouter()


@probe.get("/probe/projects/{project_id}/admin-only")
def admin_only(access: Access = Depends(require(Perm.API_KEYS))):
    return {"ok": True}


@pytest.fixture
def app(settings):
    app = create_app(settings)
    app.include_router(probe)
    return app


def assert_csp_clean(html: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline script"
    assert not re.search(r"\sstyle=", html), "inline style attribute"
    assert not re.search(r"<style", html), "inline style element"
    assert not re.search(r"\son\w+=", html), "inline event handler"


@pytest.mark.parametrize(
    "path",
    [
        "/static/app.css",
        "/static/app.js",
        "/static/vendor/htmx.min.js",
        "/static/favicon.svg",
        "/static/fonts/inter-400.woff2",
        "/static/fonts/jetbrains-mono-500.woff2",
    ],
)
def test_static_assets_are_served(client, path):
    r = client.get(path)
    assert r.status_code == 200 and len(r.content) > 100
    assert "content-security-policy" in r.headers


def test_css_has_both_themes_and_no_external_fonts(client):
    css = client.get("/static/app.css").text
    assert '[data-theme="dark"]' in css and "prefers-color-scheme:dark" in css
    assert "focus-visible" in css and ":root" in css
    assert "fonts.googleapis" not in css and "http://" not in css and "https://" not in css


def test_signed_in_404_renders_inside_the_shell(client, world, login):
    login(client, world.user(Role.VIEWER))
    r = client.get("/nothing/here", headers={"Accept": "text/html"})
    assert r.status_code == 404
    html = r.text
    assert 'class="sidenav"' in html and "Page not found" in html and "data-theme-toggle" in html
    assert world.project.name in html and f"/project/{world.project.id}/traces" in html
    assert_csp_clean(html)


def test_signed_in_403_renders_inside_the_shell(client, world, login):
    login(client, world.user(Role.MEMBER))
    r = client.get(
        f"/probe/projects/{world.project.id}/admin-only", headers={"Accept": "text/html"}
    )
    assert r.status_code == 403
    assert 'class="sidenav"' in r.text and "do not have access" in r.text
    assert_csp_clean(r.text)


def test_anonymous_404_uses_the_bare_layout(client):
    r = client.get("/nothing/here", headers={"Accept": "text/html"})
    assert r.status_code == 404 and "Page not found" in r.text
    assert 'class="sidenav"' not in r.text
    assert_csp_clean(r.text)


def test_shell_markup_and_help_dialog(client, world, login):
    login(client, world.user(Role.ADMIN))
    html = client.get("/nothing", headers={"Accept": "text/html"}).text
    for needle in (
        'aria-label="Main"',
        'id="help-dialog"',
        "Keyboard shortcuts",
        'data-nav="dashboard"',
        'data-nav="settings"',
        'class="bottomnav"',
        'href="#main"',
        "/static/vendor/htmx.min.js",
    ):
        assert needle in html, needle
    assert "includeIndicatorStyles" in html  # htmx must not inject inline styles


def test_user_content_is_escaped_in_the_shell(
    client, world, login, make_user, make_membership, make_org, make_project, db
):
    evil = "<script>alert(1)</script>"
    org = make_org(evil)
    make_project(org, evil)
    user = world.user(Role.ADMIN)
    make_membership(user, org, Role.OWNER)
    login(client, user)
    html = client.get("/nothing", headers={"Accept": "text/html"}).text
    assert evil not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_404_copy_has_a_heading_and_separate_paragraphs(client, world, login):
    login(client, world.user(Role.VIEWER))
    html = client.get("/nothing/here", headers={"Accept": "text/html"}).text
    assert "Error 404" in html and "<h1>Page not found</h1>" in html
    assert "Not Found" not in html  # the framework's default detail is not echoed
    paragraphs = re.findall(r"<p(?:\s[^>]*)?>(.*?)</p>", html, flags=re.S)
    assert "There is nothing at this address." in paragraphs
    assert "The link may be old, or the item may have been deleted." in paragraphs


def test_shell_forms_have_a_token_too(client, world, login):
    login(client, world.user(Role.ADMIN))
    html = client.get(f"/project/{world.project.id}/settings/members").text
    tokens = re.findall(r'name="csrf_token" value="([^"]*)"', html)
    assert tokens and all(
        t == client.cookies["tl_csrf"] for t in tokens
    )  # logout, role, invite, ...


def test_narrow_screen_rules_for_tabs_and_member_rows(client, world, login):
    css = client.get("/static/app.css").text
    narrow = css[css.rindex("@container app (max-width:760px){\n  .tabs{") :]
    assert (
        ".tabs{flex-wrap:wrap" in narrow and ".tbl.stack-m tr{display:flex;flex-wrap:wrap" in narrow
    )
    login(client, world.user(Role.ADMIN))
    html = client.get(f"/project/{world.project.id}/settings/members").text
    assert (
        html.count('class="tbl tbl-flat stack-m"') >= 1
    )  # members stack inside the card, not off to the side
