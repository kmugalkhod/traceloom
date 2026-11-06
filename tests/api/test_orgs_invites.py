from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import (
    ApiKey,
    AuditLog,
    Invite,
    Membership,
    Observation,
    Organization,
    Project,
    Trace,
    User,
)
from traceloom.services.accounts import hash_token

H = {"Accept": "text/html"}


def members_url(world):
    return f"/project/{world.project.id}/settings/members"


def role_of(db, user, org):
    db.expire_all()
    m = db.get(Membership, (user.id, org.id))
    return m.role if m else None


def post_role(client, world, target, role, **kw):
    return client.post(
        f"/orgs/{world.org.id}/members/{target.id}/role",
        data={"role": role, "back": members_url(world)},
        follow_redirects=False,
        **kw,
    )


def invite(client, world, email, role="member"):
    return client.post(
        f"/orgs/{world.org.id}/invites",
        data={"email": email, "role": role, "back": members_url(world)},
        follow_redirects=False,
    )


def invite_token(container):
    return re.search(r"/invite/([\w-]+)", container.mailer.outbox[-1].body).group(1)


# --- creating organizations and projects ---


def test_creating_an_org_makes_the_creator_owner(client, make_user, login, db):
    user = make_user()
    login(client, user)
    r = client.post(
        "/orgs/new",
        data={"name": "  Acme   Labs ", "project": "support-agent"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].endswith("/settings/keys")
    org = db.scalar(select(Organization))
    assert org.name == "Acme Labs"
    assert db.get(Membership, (user.id, org.id)).role == "owner"
    assert db.scalar(select(Project).where(Project.org_id == org.id)).name == "support-agent"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "org.created")).org_id == org.id


def test_empty_org_name_is_an_inline_error(client, make_user, login, db):
    login(client, make_user())
    r = client.post("/orgs/new", data={"name": "  ", "project": "p"})
    assert r.status_code == 400 and "Organization name is required." in r.text
    assert db.scalar(select(Organization)) is None


def test_a_user_can_hold_different_roles_in_several_orgs(
    client, world, make_user, make_org, make_project, make_membership, login
):
    user = world.user(Role.VIEWER)
    other = make_org("Second org")
    other_project = make_project(other, "second-project")
    make_membership(user, other, Role.OWNER)
    login(client, user)
    page = client.get(members_url(world)).text
    assert (
        "Second org" in page and "second-project" in page and "Viewer" in page and "Owner" in page
    )
    assert "Danger zone" not in page  # Viewer here...
    owner_page = client.get(f"/project/{other_project.id}/settings/members").text
    assert "Danger zone" in owner_page  # ...Owner there


def test_projects_can_be_created_by_admins_only(client, world, login, db):
    login(client, world.user(Role.MEMBER))
    assert client.post(f"/orgs/{world.org.id}/projects", data={"name": "x"}).status_code == 403
    login(client, world.user(Role.ADMIN))
    r = client.post(
        f"/orgs/{world.org.id}/projects", data={"name": "second"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert db.scalar(select(Project).where(Project.name == "second")).org_id == world.org.id


def test_project_rename_needs_admin(client, world, login, db):
    url = f"/project/{world.project.id}/settings"
    login(client, world.user(Role.MEMBER))
    assert client.post(url, data={"name": "nope"}).status_code == 403
    login(client, world.user(Role.ADMIN))
    assert client.post(url, data={"name": "renamed"}, follow_redirects=False).status_code == 303
    db.expire_all()
    assert db.get(Project, world.project.id).name == "renamed"
    assert client.post(url, data={"name": " "}).status_code == 400


# --- invites ---


def test_invite_role(client, container, world, make_user, login, db):
    invitee = make_user(email="dev@example.com")
    login(client, world.user(Role.ADMIN))
    assert invite(client, world, "Dev@Example.com", "admin").status_code == 303
    mail = container.mailer.outbox[-1]
    assert mail.to == "dev@example.com" and "as Admin" in mail.body
    token = invite_token(container)
    row = db.scalar(select(Invite))
    assert row.token_hash == hash_token(token) and token not in row.token_hash  # stored hashed
    assert row.expires_at > datetime.now(UTC) + timedelta(days=6)

    from fastapi.testclient import TestClient

    with TestClient(client.app) as browser:  # the invitee, in another browser
        login(browser, invitee)
        page = browser.get(f"/invite/{token}")
        assert page.status_code == 200 and "Join" in page.text and "Admin" in page.text
        r = browser.post(f"/invite/{token}/accept", follow_redirects=False)
        assert r.status_code == 303
    assert role_of(db, invitee, world.org) == "admin"
    db.expire_all()
    assert db.scalar(select(Invite)).accepted_at is not None


def test_new_user_is_routed_through_signup_into_the_org(client, container, world, login, db):
    from fastapi.testclient import TestClient

    login(client, world.user(Role.OWNER))
    invite(client, world, "fresh@example.com", "viewer")
    token = invite_token(container)
    with TestClient(client.app) as visitor:
        r = visitor.get(f"/invite/{token}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/signup?invite={token}"
        visitor.get("/login")
        visitor.headers["X-CSRF-Token"] = visitor.cookies["tl_csrf"]
        done = visitor.post(
            "/signup",
            data={
                "invite": token,
                "email": "fresh@example.com",
                "name": "F",
                "password": "correct horse battery",
            },
            follow_redirects=False,
        )
        assert done.status_code == 303
    user = db.scalar(select(User).where(User.email == "fresh@example.com"))
    assert role_of(db, user, world.org) == "viewer"


def test_existing_user_is_asked_to_log_in_first(client, container, world, make_user, login):
    from fastapi.testclient import TestClient

    make_user(email="dev@example.com")
    login(client, world.user(Role.OWNER))
    invite(client, world, "dev@example.com")
    token = invite_token(container)
    with TestClient(client.app) as visitor:
        r = visitor.get(f"/invite/{token}", follow_redirects=False)
        assert r.headers["location"] == f"/login?next=/invite/{token}"


def test_expired_used_and_unknown_tokens_fail(client, container, world, make_user, login, db):
    invitee = make_user(email="dev@example.com")
    login(client, world.user(Role.OWNER))
    invite(client, world, "dev@example.com")
    token = invite_token(container)
    login(client, invitee)
    assert client.post(f"/invite/{token}/accept", follow_redirects=False).status_code == 303
    again = client.post(f"/invite/{token}/accept")
    assert again.status_code == 410 and "Invite expired" in again.text  # used
    assert client.get("/invite/unknown-token").status_code == 410
    row = db.scalar(select(Invite))
    row.accepted_at, row.expires_at = None, datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert client.get(f"/invite/{token}").status_code == 410  # expired


def test_invite_for_another_address_cannot_be_accepted(
    client, container, world, make_user, login, db
):
    login(client, world.user(Role.OWNER))
    invite(client, world, "dev@example.com")
    token = invite_token(container)
    intruder = make_user(email="intruder@example.com")
    login(client, intruder)
    assert client.get(f"/invite/{token}").status_code == 403
    assert client.post(f"/invite/{token}/accept").status_code == 403
    assert role_of(db, intruder, world.org) is None


def test_reinviting_replaces_the_pending_invite_and_members_cannot_be_invited(
    client, container, world, login, db
):
    login(client, world.user(Role.ADMIN))
    invite(client, world, "dev@example.com")
    invite(client, world, "dev@example.com", "viewer")
    rows = db.scalars(select(Invite)).all()
    assert len(rows) == 1 and rows[0].role == "viewer"
    r = invite(client, world, world.user(Role.VIEWER).email)
    assert r.status_code == 400 and "already a member" in r.text
    assert invite(client, world, "not an email").status_code == 400


def test_invites_can_be_revoked(client, container, world, login, db):
    login(client, world.user(Role.ADMIN))
    invite(client, world, "dev@example.com")
    inv = db.scalar(select(Invite))
    r = client.post(
        f"/orgs/{world.org.id}/invites/{inv.id}/revoke",
        data={"back": members_url(world)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.scalar(select(Invite)) is None
    assert client.get(f"/invite/{invite_token(container)}").status_code == 410


# --- roles ---


def test_role_change_is_audited(client, world, login, db):
    login(client, world.user(Role.ADMIN))
    r = post_role(client, world, world.user(Role.VIEWER), "member")
    assert r.status_code == 303 and "notice=role" in r.headers["location"]
    assert role_of(db, world.user(Role.VIEWER), world.org) == "member"
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "member.role_changed"))
    assert (
        audit.detail == {"from": "viewer", "to": "member"}
        and audit.target == world.user(Role.VIEWER).id
    )
    assert audit.actor_id == world.user(Role.ADMIN).id and audit.org_id == world.org.id


def test_last_owner_protected(client, world, login, db):
    owner = world.user(Role.OWNER)
    login(client, owner)
    demote = post_role(client, world, owner, "admin")
    assert demote.status_code == 400 and "last Owner cannot be demoted" in demote.text
    assert role_of(db, owner, world.org) == "owner"
    leave = client.post(
        f"/orgs/{world.org.id}/members/{owner.id}/remove", data={"back": members_url(world)}
    )
    assert leave.status_code == 400 and "last Owner cannot leave" in leave.text
    assert role_of(db, owner, world.org) == "owner"
    # With a second Owner, the first can step down and leave.
    assert post_role(client, world, world.user(Role.ADMIN), "owner").status_code == 303
    assert post_role(client, world, owner, "member").status_code == 303
    assert role_of(db, owner, world.org) == "member"


def test_an_owner_can_leave_once_another_owner_exists(client, world, login, db):
    second = world.user(Role.ADMIN)
    owner = world.user(Role.OWNER)
    login(client, owner)
    post_role(client, world, second, "owner")
    r = client.post(
        f"/orgs/{world.org.id}/members/{owner.id}/remove",
        data={"back": members_url(world)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and role_of(db, owner, world.org) is None


def test_anyone_can_leave_but_not_remove_others(client, world, login, db):
    viewer, member = world.user(Role.VIEWER), world.user(Role.MEMBER)
    login(client, viewer)
    assert (
        client.post(f"/orgs/{world.org.id}/members/{member.id}/remove", data={}).status_code == 403
    )
    assert (
        client.post(
            f"/orgs/{world.org.id}/members/{viewer.id}/remove", data={}, follow_redirects=False
        ).status_code
        == 303
    )
    assert role_of(db, viewer, world.org) is None and role_of(db, member, world.org) == "member"


def test_admin_cannot_grant_owner_demote_owners_or_delete_the_org(
    client, container, world, login, db
):
    admin, owner, viewer = world.user(Role.ADMIN), world.user(Role.OWNER), world.user(Role.VIEWER)
    login(client, admin)
    assert post_role(client, world, viewer, "owner").status_code == 403
    assert post_role(client, world, admin, "owner").status_code == 403
    assert post_role(client, world, owner, "viewer").status_code == 403
    assert (
        client.post(f"/orgs/{world.org.id}/members/{owner.id}/remove", data={}).status_code == 403
    )
    assert invite(client, world, "x@example.com", "owner").status_code == 403
    assert (
        client.post(
            f"/orgs/{world.org.id}/delete", data={"confirm_name": world.org.name}
        ).status_code
        == 403
    )
    assert db.get(Organization, world.org.id) is not None
    assert role_of(db, viewer, world.org) == "viewer" and role_of(db, owner, world.org) == "owner"
    assert post_role(client, world, viewer, "admin").status_code == 303  # what an Admin can do
    assert (
        client.post(
            f"/orgs/{world.org.id}/members/{world.user(Role.MEMBER).id}/remove",
            data={},
            follow_redirects=False,
        ).status_code
        == 303
    )


def test_unknown_roles_and_members_are_rejected(client, world, login):
    login(client, world.user(Role.OWNER))
    assert post_role(client, world, world.user(Role.VIEWER), "superuser").status_code == 400
    assert (
        client.post(
            f"/orgs/{world.org.id}/members/nobody/role", data={"role": "member"}
        ).status_code
        == 400
    )


def test_outsiders_get_404(client, world, make_user, login):
    login(client, make_user())
    assert client.get(members_url(world)).status_code == 404
    assert client.post(f"/orgs/{world.org.id}/members/x/remove", data={}).status_code == 404


# --- delete organization ---


def test_owner_deletes_the_org_with_everything_in_it(client, container, world, make_key, login, db):
    key, public, secret = make_key(world.project)
    with container.session_factory.begin() as s:
        s.add(Trace(project_id=world.project.id, id="t1", timestamp=datetime.now(UTC)))
        s.add(
            Observation(
                project_id=world.project.id, id="o1", trace_id="t1", start_time=datetime.now(UTC)
            )
        )
    from traceloom.auth.api_keys import ResolvedKey, hash_secret

    container.api_key_cache.put(
        public,
        hash_secret(secret, container.settings.secret_key),
        ResolvedKey(key.id, world.project.id, public),
    )
    org_id, org_name = world.org.id, world.org.name
    login(client, world.user(Role.OWNER))
    wrong = client.post(
        f"/orgs/{org_id}/delete", data={"confirm_name": "nope", "back": members_url(world)}
    )
    assert wrong.status_code == 400 and "Type the organization name" in wrong.text
    assert db.get(Organization, org_id) is not None
    r = client.post(
        f"/orgs/{org_id}/delete",
        data={"confirm_name": org_name},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Organization, org_id) is None
    assert db.scalar(select(Project)) is None and db.scalar(select(ApiKey)) is None
    assert db.scalar(select(Membership)) is None
    assert db.scalar(select(Trace)) is None and db.scalar(select(Observation)) is None
    assert (
        container.api_key_cache.get(public, hash_secret(secret, container.settings.secret_key))
        is None
    )
    assert (
        db.scalar(select(AuditLog).where(AuditLog.action == "org.deleted")).detail["projects"] == 1
    )


# --- what each role sees ---


@pytest.mark.parametrize(
    ("role", "invite_panel", "role_selects", "danger", "owner_option"),
    [
        (Role.VIEWER, False, False, False, False),
        (Role.MEMBER, False, False, False, False),
        (Role.ADMIN, True, True, False, False),
        (Role.OWNER, True, True, True, True),
    ],
)
def test_controls_are_not_rendered_for_roles_without_the_permission(
    client, world, login, role, invite_panel, role_selects, danger, owner_option
):
    login(client, world.user(role))
    html = client.get(members_url(world), headers=H).text
    assert ("Invite someone" in html) is invite_panel
    assert ("data-autosubmit" in html) is role_selects
    assert ("Danger zone" in html) is danger
    assert ('<option value="owner"' in html) is owner_option
    assert "disabled" not in re.sub(r"<noscript>.*?</noscript>", "", html)  # hidden, never disabled
    assert world.user(Role.VIEWER).email in html  # everyone can read the list


def test_admin_sees_remove_for_non_owners_only(client, world, login):
    login(client, world.user(Role.ADMIN))
    html = client.get(members_url(world)).text
    assert html.count(">Remove<") == 2  # viewer and member; not the owner or themselves
    login(client, world.user(Role.OWNER))
    assert "Last Owner" in client.get(members_url(world)).text


def test_member_names_are_escaped(client, world, login, db):
    evil = "<script>alert(1)</script>"
    world.user(Role.VIEWER).name = evil
    db.commit()
    login(client, world.user(Role.OWNER))
    html = client.get(members_url(world)).text
    assert evil not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
