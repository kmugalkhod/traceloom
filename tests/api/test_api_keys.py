from __future__ import annotations

import base64
import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import ApiKey, AuditLog

H = {"Accept": "text/html"}


def keys_url(world):
    return f"/project/{world.project.id}/settings/keys"


def basic(public, secret):
    return {"Authorization": "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()}


def create(client, world, note="Demo agent"):
    return client.post(keys_url(world), data={"note": note})


def pair(html):
    return (
        re.search(r'id="new-public">(tl-pk-[\w-]+)<', html).group(1),
        re.search(r'id="new-secret">(tl-sk-[\w-]+)<', html).group(1),
    )


BATCH = {
    "batch": [
        {
            "id": "e1",
            "type": "trace-create",
            "timestamp": datetime.now(UTC).isoformat(),
            "body": {"id": "t1", "name": "demo"},
        }
    ]
}


def test_secret_is_shown_once_and_never_stored(client, world, login, db):
    login(client, world.user(Role.ADMIN))
    r = create(client, world)
    assert r.status_code == 201 and "Copy the secret key now" in r.text
    assert r.headers["cache-control"] == "no-store"
    public, secret = pair(r.text)
    key = db.scalar(select(ApiKey).where(ApiKey.public_key == public))
    assert key.note == "Demo agent" and key.project_id == world.project.id
    stored = " ".join(str(getattr(key, c.name)) for c in ApiKey.__table__.columns)
    assert secret not in stored and key.secret_hash not in secret  # only a hash is kept
    for row in db.scalars(select(AuditLog)):  # nor does the audit log
        assert secret not in str(row.detail) and secret not in row.target
    listing = client.get(keys_url(world), headers=H)
    assert public in listing.text and secret not in listing.text and "Active" in listing.text
    assert (
        "cache-control" not in listing.headers or "no-store" not in listing.headers["cache-control"]
    )


def test_created_key_can_ingest_and_revoking_stops_it_at_once(client, world, login, db):
    login(client, world.user(Role.ADMIN))
    public, secret = pair(create(client, world).text)
    ok = client.post("/api/public/ingestion", json=BATCH, headers=basic(public, secret))
    assert ok.status_code == 207  # also warms the key cache
    key = db.scalar(select(ApiKey).where(ApiKey.public_key == public))
    assert key.last_used_at is not None
    r = client.post(f"{keys_url(world)}/{key.id}/revoke", follow_redirects=False)
    assert r.status_code == 303 and "notice=revoked" in r.headers["location"]
    assert (
        client.post("/api/public/ingestion", json=BATCH, headers=basic(public, secret)).status_code
        == 401
    )
    page = client.get(keys_url(world), headers=H).text
    assert "Revoked" in page and "Rejected with 401" in page and "Key revoked" not in page
    assert db.scalar(select(AuditLog).where(AuditLog.action == "api_key.revoked")).detail == {
        "public_key": public
    }
    assert (
        client.post(f"{keys_url(world)}/{key.id}/revoke", follow_redirects=False).status_code == 303
    )  # idempotent


@pytest.mark.parametrize("role", [Role.VIEWER, Role.MEMBER])
def test_members_and_viewers_get_403(client, world, login, make_key, role):
    key, _, _ = make_key(world.project)
    login(client, world.user(role))
    assert client.get(keys_url(world), headers=H).status_code == 403
    assert create(client, world).status_code == 403
    assert client.post(f"{keys_url(world)}/{key.id}/revoke").status_code == 403
    page = client.get(f"/project/{world.project.id}/settings", headers=H).text
    assert (
        "API keys" not in page.split('role="tablist"')[1].split("</div>")[0]
    )  # the tab is not offered


def test_admins_and_owners_can_manage_keys(client, world, login):
    for role in (Role.ADMIN, Role.OWNER):
        login(client, world.user(role))
        assert create(client, world).status_code == 201


def test_a_key_of_another_project_cannot_be_revoked(
    client, world, login, make_project, make_key, db
):
    other = make_project(world.org)
    key, public, secret = make_key(other)
    login(client, world.user(Role.OWNER))
    assert client.post(f"{keys_url(world)}/{key.id}/revoke").status_code == 404
    assert client.post(f"{keys_url(world)}/missing/revoke").status_code == 404
    db.expire_all()
    assert db.get(ApiKey, key.id).revoked_at is None
    assert (
        public not in client.get(keys_url(world), headers=H).text
    )  # other projects' keys are not listed


def test_outsiders_cannot_see_keys(client, world, make_user, login):
    login(client, make_user())
    assert client.get(keys_url(world), headers=H).status_code == 404


def test_empty_project_shows_the_onboarding_card(client, world, login, make_key):
    login(client, world.user(Role.ADMIN))
    page = client.get(keys_url(world), headers=H).text
    assert (
        "Create an API key" in page
        and "TRACELOOM_PUBLIC_KEY" in page
        and "from traceloom_sdk import" in page
    )
    assert 'id="sdk-snippet"' in page and 'data-copy-from="#sdk-snippet"' in page
    make_key(world.project)
    page = client.get(keys_url(world), headers=H).text
    assert 'id="sdk-snippet"' not in page and "Public key" in page


def test_onboarding_one_click_creates_the_first_key(client, world, login, db):
    login(client, world.user(Role.ADMIN))
    r = client.post(keys_url(world), data={"note": "First key"})
    assert r.status_code == 201 and db.scalar(select(ApiKey)).note == "First key"


def test_long_notes_are_rejected_inline(client, world, login, db):
    login(client, world.user(Role.ADMIN))
    r = create(client, world, note="x" * 201)
    assert r.status_code == 400 and "200 characters" in r.text
    assert db.scalar(select(ApiKey)) is None


def test_notes_are_escaped(client, world, login):
    login(client, world.user(Role.ADMIN))
    create(client, world, note="<img src=x onerror=alert(1)>")
    html = client.get(keys_url(world), headers=H).text
    assert "<img src=x" not in html and "&lt;img src=x" in html


def test_last_used_is_committed_on_its_own_not_with_the_request(client, world, make_key, container):
    """The stamp is a short transaction of its own: a request that fails (and rolls back its
    session) still records it, and no write lock is held while the request runs."""
    key, public, secret = make_key(world.project)
    r = client.post(
        "/api/public/ingestion",
        content=b"not json",
        headers={**basic(public, secret), "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    with container.session_factory() as s:
        assert s.get(ApiKey, key.id).last_used_at is not None
