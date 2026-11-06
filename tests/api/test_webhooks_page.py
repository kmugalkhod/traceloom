from __future__ import annotations

import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.base import utcnow
from traceloom.db.models import AuditLog, Webhook, WebhookDelivery
from traceloom.web.routers import settings_webhooks
from traceloom.webhooks import events

HTML = {"Accept": "text/html"}
XSS = "<script>alert(1)</script>"
PUBLIC = "https://93.184.216.34/hook"  # an IP literal: no DNS lookup in the test


@pytest.fixture
def kicks(monkeypatch):
    calls = []
    monkeypatch.setattr(settings_webhooks.deliver, "kick", lambda container: calls.append(1) or 0)
    return calls


def url(world, suffix=""):
    return f"/project/{world.project.id}/settings/webhooks{suffix}"


def token(client, world):
    page = client.get(url(world), headers=HTML)
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def post(client, world, suffix="", **data):
    data["csrf_token"] = token(client, world)
    return client.post(url(world, suffix), data=data, headers=HTML, follow_redirects=False)


@pytest.fixture
def admin(client, login, world):
    return login(client, world.users[Role.ADMIN])


def add(db, world, hook_url=PUBLIC, **kw):
    row = Webhook(
        project_id=world.project.id,
        url=hook_url,
        events=kw.pop("events", [events.LABEL_MOVED]),
        enabled=kw.pop("enabled", True),
        secret_ciphertext="x",
    )
    db.add(row)
    db.commit()
    return row


def delivery(db, hook, **kw):
    row = WebhookDelivery(
        webhook_id=hook.id,
        project_id=hook.project_id,
        event=kw.pop("event", events.LABEL_MOVED),
        payload={},
        created_at=kw.pop("created_at", utcnow()),
        **kw,
    )
    db.add(row)
    db.commit()
    return row


# --- access ---------------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.MEMBER, Role.VIEWER])
def test_member_and_viewer_get_403_and_no_tab(client, login, world, role, db, kicks):
    hook = add(db, world)
    login(client, world.users[role])
    assert client.get(url(world), headers=HTML).status_code == 403
    for method, suffix in (
        ("post", ""),
        ("post", f"/{hook.id}"),
        ("post", f"/{hook.id}/test"),
        ("post", f"/{hook.id}/toggle"),
        ("post", f"/{hook.id}/delete"),
    ):
        r = getattr(client, method)(url(world, suffix), data={"url": PUBLIC}, headers=HTML)
        assert r.status_code == 403, suffix
    settings = client.get(f"/project/{world.project.id}/settings", headers=HTML).text
    assert "Webhooks" not in settings and kicks == []


def test_admin_sees_the_tab(admin, world):
    page = admin.get(f"/project/{world.project.id}/settings", headers=HTML).text
    assert url(world) in page and "Webhooks" in page


def test_empty_state(admin, world):
    page = admin.get(url(world), headers=HTML).text
    assert "No webhooks yet" in page and "X-Traceloom-Signature" in page


# --- create ---------------------------------------------------------------------------------


def test_admin_adds_a_webhook_and_sees_the_secret_once(admin, world, db):
    r = post(
        admin, world, url=PUBLIC, events=[events.LABEL_MOVED, events.VERSION_CREATED], enabled="1"
    )
    assert r.status_code == 201
    secret = re.search(r"whsec_[A-Za-z0-9_\-]+", r.text).group(0)
    assert "data-autoopen" in r.text and "shown once" in r.text
    assert r.headers["cache-control"] == "no-store"
    hook = db.scalar(select(Webhook))
    assert (
        hook.url == PUBLIC
        and hook.enabled
        and hook.events == [events.LABEL_MOVED, events.VERSION_CREATED]
    )
    assert secret not in hook.secret_ciphertext  # stored encrypted
    again = admin.get(url(world), headers=HTML).text
    assert secret not in again and "whsec_" not in again
    assert PUBLIC in again and "Label moved, Version created" in again
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "webhook.created"))
    assert audit is not None and secret not in str(audit.detail)


def test_private_url_is_an_inline_error(admin, world, db):
    r = post(admin, world, url="http://127.0.0.1:9/hook", events=[events.LABEL_MOVED])
    assert r.status_code == 400
    assert "not allowed" in r.text and 'role="alert"' in r.text and "data-autoopen" in r.text
    assert 'value="http://127.0.0.1:9/hook"' in r.text  # what they typed is kept
    assert db.scalar(select(Webhook)) is None and "whsec_" not in r.text


def test_private_url_is_allowed_when_the_operator_opts_in(admin, world, db, container):
    container.settings.webhook_allow_private = True
    r = post(admin, world, url="http://127.0.0.1:9/hook", events=[events.LABEL_MOVED])
    assert r.status_code == 201 and db.scalar(select(Webhook)).url == "http://127.0.0.1:9/hook"


def test_bad_input_errors(admin, world, db):
    assert (
        "must start with https"
        in post(admin, world, url="ftp://x.example/h", events=[events.LABEL_MOVED]).text
    )
    assert "Pick at least one" in post(admin, world, url=PUBLIC).text
    assert "Enter a URL" in post(admin, world, url="", events=[events.LABEL_MOVED]).text
    assert db.scalar(select(Webhook)) is None


# --- list, status, deliveries ---------------------------------------------------------------


def test_rows_show_status_pills_from_the_latest_delivery(admin, world, db):
    ok, retry, off, fresh = (
        add(db, world, f"https://93.184.216.{i}/h", enabled=i != 3) for i in range(1, 5)
    )
    now = utcnow()
    delivery(db, ok, status="succeeded", attempts=1, last_status_code=200, created_at=now)
    delivery(
        db,
        retry,
        status="pending",
        attempts=2,
        last_status_code=503,
        created_at=now,
        next_attempt_at=now + timedelta(seconds=38),
    )
    page = admin.get(url(world), headers=HTML).text
    assert "Delivered" in page and "Retrying 2 of 5" in page
    assert "Disabled" in page and "No deliveries yet" in page and fresh and off


def test_deliveries_table_shows_status_attempts_code_and_error(admin, world, db):
    hook = add(db, world, "https://93.184.216.1/h")
    now = utcnow()
    delivery(db, hook, status="succeeded", attempts=1, last_status_code=200, created_at=now)
    delivery(
        db,
        hook,
        status="failed",
        attempts=5,
        last_error="Timed out after 10 s",
        created_at=now - timedelta(minutes=1),
        event=events.VERSION_CREATED,
    )
    delivery(
        db,
        hook,
        status="pending",
        attempts=2,
        last_status_code=503,
        created_at=now - timedelta(minutes=2),
        next_attempt_at=now + timedelta(seconds=40),
    )
    page = admin.get(url(world), headers=HTML).text
    assert "200 · 1 attempt" in page
    assert "Timed out after 10 s · 5 attempts" in page and "Failed" in page
    assert "503 · next try in" in page and "Retrying 2 of 5" in page
    assert "prompt.label_moved" in page and "93.184.216.1" in page


def test_last_error_and_url_are_escaped(admin, world, db):
    hook = add(db, world, f"https://93.184.216.1/{XSS}")
    delivery(db, hook, status="failed", attempts=5, last_error=XSS)
    page = admin.get(url(world), headers=HTML).text
    assert XSS not in page and "&lt;script&gt;" in page


def test_only_this_projects_webhooks_are_listed(admin, world, db, make_project):
    other = make_project(world.org)
    foreign = Webhook(
        project_id=other.id,
        url="https://93.184.216.99/x",
        events=[events.LABEL_MOVED],
        secret_ciphertext="x",
    )
    db.add(foreign)
    db.commit()
    delivery(db, foreign, status="succeeded", attempts=1, last_status_code=200)
    page = admin.get(url(world), headers=HTML).text
    assert "93.184.216.99" not in page
    assert (
        admin.post(
            url(world, f"/{foreign.id}/test"),
            data={"csrf_token": token(admin, world)},
            headers=HTML,
        ).status_code
        == 404
    )


# --- send test, edit, toggle, delete --------------------------------------------------------


def test_send_test_creates_a_delivery_and_kicks_the_queue(admin, world, db, kicks):
    hook = add(db, world, enabled=False)  # a test goes out even when the webhook is disabled
    r = post(admin, world, f"/{hook.id}/test")
    assert r.status_code == 303 and "notice=tested" in r.headers["location"]
    row = db.scalar(select(WebhookDelivery).where(WebhookDelivery.webhook_id == hook.id))
    assert row.event == "webhook.test" and row.status == "pending"
    assert kicks == [1]  # delivered now, not at the next sweep
    page = admin.get(r.headers["location"], headers=HTML).text
    assert "Test delivery queued" in page and "webhook.test" in page and "Queued" in page


def test_edit_validates_the_url_again(admin, world, db):
    hook = add(db, world)
    bad = post(
        admin,
        world,
        f"/{hook.id}",
        url="http://127.0.0.1:9/x",
        events=[events.LABEL_MOVED],
        enabled="1",
    )
    assert bad.status_code == 400 and "not allowed" in bad.text and "data-autoopen" in bad.text
    db.refresh(hook)
    assert hook.url == PUBLIC
    ok = post(
        admin, world, f"/{hook.id}", url="https://93.184.216.5/new", events=[events.VERSION_CREATED]
    )
    assert ok.status_code == 303
    db.refresh(hook)
    assert (
        hook.url.endswith("/new")
        and hook.events == [events.VERSION_CREATED]
        and hook.enabled is False
    )


def test_toggle_and_delete(admin, world, db):
    hook = add(db, world)
    post(admin, world, f"/{hook.id}/toggle")
    db.refresh(hook)
    assert hook.enabled is False
    assert "Enable" in admin.get(url(world), headers=HTML).text
    post(admin, world, f"/{hook.id}/toggle")
    db.refresh(hook)
    assert hook.enabled is True
    r = post(admin, world, f"/{hook.id}/delete")
    assert r.status_code == 303 and db.scalar(select(Webhook)) is None
    actions = [
        a.action for a in db.scalars(select(AuditLog).where(AuditLog.action.like("webhook.%")))
    ]
    assert "webhook.deleted" in actions and actions.count("webhook.updated") == 2


def test_unknown_webhook_is_404(admin, world):
    for suffix in ("/nope/test", "/nope/toggle", "/nope/delete", "/nope"):
        assert (
            post(admin, world, suffix, url=PUBLIC, events=[events.LABEL_MOVED]).status_code == 404
        )


def test_forms_need_a_csrf_token(client, login, world):
    login(client, world.users[Role.ADMIN], csrf=False)
    r = client.post(url(world), data={"url": PUBLIC}, headers=HTML)
    assert r.status_code == 403
