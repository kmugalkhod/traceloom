from __future__ import annotations

import base64
from urllib.parse import quote

import pytest
from sqlalchemy import select

from traceloom.auth.permissions import Role
from traceloom.db.models import ApiKey, AuditLog

BASE = "/api/public/v2/prompts"


def basic(public, secret):
    return {"Authorization": "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()}


@pytest.fixture
def auth(make_key, world):
    _, public, secret = make_key(world.project)
    return basic(public, secret)


@pytest.fixture
def admin_auth(make_key, world, db):
    row, public, secret = make_key(world.project)
    row.may_move_protected_labels = True
    db.commit()
    return basic(public, secret)


def create(client, auth, name="support", prompt="Hello {{name}}", **extra):
    body = {"name": name, "type": "text", "prompt": prompt, "commitMessage": "init", **extra}
    return client.post(BASE, json=body, headers=auth)


# --- auth ---------------------------------------------------------------------------------


def test_requires_a_project_key(client):
    assert client.get(f"{BASE}/support").status_code == 401
    bad = client.get(f"{BASE}/support", headers=basic("tl-pk-x", "tl-sk-y"))
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "unauthorized"
    assert client.post(BASE, json={}).status_code == 401


# --- create / get ---------------------------------------------------------------------------


def test_create_and_get_shape(client, auth):
    r = create(client, auth, config={"model": "m"}, tags=["a"], labels=["staging"])
    assert r.status_code == 201
    got = client.get(f"{BASE}/support", headers=auth).json()  # no production: latest
    assert got["name"] == "support" and got["type"] == "text" and got["version"] == 1
    assert got["prompt"] == "Hello {{name}}" and got["config"] == {"model": "m"}
    assert sorted(got["labels"]) == ["latest", "staging"] and got["tags"] == ["a"]
    assert got["commitMessage"] == "init" and got["dependencies"] == []


def test_get_defaults_to_production_else_latest(client, auth, admin_auth):
    create(client, auth, prompt="one")
    create(client, auth, prompt="two")
    assert client.get(f"{BASE}/support", headers=auth).json()["prompt"] == "two"
    r = client.patch(
        f"{BASE}/support/versions/1", json={"newLabels": ["production"]}, headers=admin_auth
    )
    assert r.status_code == 200 and "production" in r.json()["labels"]
    assert client.get(f"{BASE}/support", headers=auth).json()["prompt"] == "one"
    assert client.get(f"{BASE}/support?label=latest", headers=auth).json()["prompt"] == "two"
    assert client.get(f"{BASE}/support?version=2", headers=auth).json()["prompt"] == "two"


def test_chat_prompt(client, auth):
    msgs = [{"role": "system", "content": "Hi {{n}}"}, {"role": "user", "content": "{{q}}"}]
    r = client.post(
        BASE,
        json={"name": "chatty", "type": "chat", "prompt": msgs, "commitMessage": "c"},
        headers=auth,
    )
    assert r.status_code == 201
    got = client.get(f"{BASE}/chatty", headers=auth).json()
    assert got["type"] == "chat" and got["prompt"] == msgs


def test_not_found_and_bad_selectors(client, auth):
    create(client, auth)
    missing = client.get(f"{BASE}/ghost", headers=auth)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert client.get(f"{BASE}/support?label=nope", headers=auth).status_code == 404
    assert client.get(f"{BASE}/support?version=9", headers=auth).status_code == 404
    assert client.get(f"{BASE}/support?label=a&version=1", headers=auth).status_code == 400
    assert client.get(f"{BASE}/support?version=x", headers=auth).status_code == 422


def test_validation_errors_use_the_envelope(client, auth):
    for body in (
        {"name": "bad name", "type": "text", "prompt": "x", "commitMessage": "m"},
        {"name": "ok", "type": "text", "prompt": "x"},  # commit message required
        {"name": "ok", "type": "video", "prompt": "x", "commitMessage": "m"},
        {"name": "ok", "type": "text", "prompt": ["x"], "commitMessage": "m"},
        {"name": "ok", "type": "text", "prompt": "x", "commitMessage": "m", "labels": ["latest"]},
    ):
        r = client.post(BASE, json=body, headers=auth)
        assert r.status_code == 400, body
        assert set(r.json()["error"]) == {"code", "message"}
    assert client.post(BASE, json={"prompt": "x"}, headers=auth).status_code == 422
    assert (
        client.post(BASE, json={"name": "a", "prompt": "x", "bogus": 1}, headers=auth).status_code
        == 422
    )


# --- names with a slash ---------------------------------------------------------------------


@pytest.mark.parametrize("path", ["support/tone", "support%2Ftone"])
def test_names_with_slash(client, auth, admin_auth, path):
    create(client, auth, name="support/tone", prompt="Be kind.")
    assert client.get(f"{BASE}/{path}", headers=auth).json()["name"] == "support/tone"
    r = client.patch(f"{BASE}/{path}/versions/1", json={"newLabels": ["staging"]}, headers=auth)
    assert r.status_code == 200 and "staging" in r.json()["labels"]


def test_url_encoded_name_helper(client, auth):
    create(client, auth, name="a/b/c", prompt="x")
    assert client.get(f"{BASE}/{quote('a/b/c', safe='')}", headers=auth).status_code == 200


# --- composition ----------------------------------------------------------------------------


def test_resolve_and_dependencies(client, auth):
    create(client, auth, name="tone", prompt="Be kind.", labels=["live"])
    create(client, auth, name="main", prompt="Rules: @@@prompt:tone|label=live@@@")
    resolved = client.get(f"{BASE}/main", headers=auth).json()
    assert resolved["prompt"] == "Rules: Be kind."
    assert resolved["dependencies"] == [{"name": "tone", "version": 1, "label": "live"}]
    raw = client.get(f"{BASE}/main?resolve=false", headers=auth).json()
    assert raw["prompt"] == "Rules: @@@prompt:tone|label=live@@@"


def test_cyclic_include_is_400_naming_the_cycle(client, auth):
    create(client, auth, name="a", prompt="A", labels=["x"])
    create(client, auth, name="b", prompt="B @@@prompt:a|label=x@@@", labels=["x"])
    r = create(client, auth, name="a", prompt="A @@@prompt:b|label=x@@@", labels=["x"])
    assert r.status_code == 400
    assert "Circular" in r.json()["error"]["message"]
    assert "@@@prompt:a|label=x@@@" in r.json()["error"]["message"]
    assert "a v2 -> b v1 -> a v2" in r.json()["error"]["message"]  # the path
    assert client.get(f"{BASE}/a?label=x", headers=auth).json()["prompt"] == "A"


def test_saving_a_missing_reference_is_400(client, auth):
    r = create(client, auth, prompt="@@@prompt:ghost|label=x@@@")
    assert r.status_code == 400 and "@@@prompt:ghost|label=x@@@" in r.json()["error"]["message"]


# --- protected labels -----------------------------------------------------------------------


def test_plain_key_cannot_move_production(client, auth, db):
    create(client, auth)
    r = client.patch(f"{BASE}/support/versions/1", json={"newLabels": ["production"]}, headers=auth)
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert client.get(f"{BASE}/support", headers=auth).json()["labels"] == ["latest"]
    assert db.scalars(select(AuditLog).where(AuditLog.action == "prompt.label_moved")).all() == []


def test_failed_patch_is_all_or_nothing(client, auth, db):
    create(client, auth)
    r = client.patch(
        f"{BASE}/support/versions/1", json={"newLabels": ["staging", "production"]}, headers=auth
    )
    assert r.status_code == 403
    assert "staging" not in client.get(f"{BASE}/support", headers=auth).json()["labels"]


def test_plain_key_cannot_set_production_while_creating(client, auth):
    r = create(client, auth, labels=["production"])
    assert r.status_code == 403
    assert client.get(f"{BASE}/support", headers=auth).status_code == 404


def test_flagged_key_moves_production_and_is_audited(client, auth, admin_auth, db):
    create(client, auth)
    r = client.patch(
        f"{BASE}/support/versions/1", json={"newLabels": ["production"]}, headers=admin_auth
    )
    assert r.status_code == 200
    (row,) = db.scalars(select(AuditLog).where(AuditLog.action == "prompt.label_moved")).all()
    assert row.actor_type == "api_key" and row.detail["label"] == "production"
    key_id = db.scalar(select(ApiKey.id).where(ApiKey.may_move_protected_labels.is_(True)))
    assert row.actor_id == key_id


def test_patch_unknown_version_is_404(client, auth):
    create(client, auth)
    r = client.patch(f"{BASE}/support/versions/7", json={"newLabels": ["staging"]}, headers=auth)
    assert r.status_code == 404
    assert (
        client.patch(
            f"{BASE}/ghost/versions/1", json={"newLabels": ["a"]}, headers=auth
        ).status_code
        == 404
    )
    assert client.patch(f"{BASE}/support/versions/1", json={}, headers=auth).status_code == 422


# --- list -----------------------------------------------------------------------------------


def test_list_with_filters_and_paging(client, auth):
    create(client, auth, name="support/a", tags=["cs"], labels=["staging"])
    create(client, auth, name="support/b", tags=["cs"])
    create(client, auth, name="sales")
    create(client, auth, name="sales", prompt="v2")
    body = client.get(BASE, headers=auth).json()
    assert [d["name"] for d in body["data"]] == ["sales", "support/a", "support/b"]
    assert body["meta"] == {"page": 1, "limit": 50, "totalItems": 3, "totalPages": 1}
    sales = body["data"][0]
    assert sales["versions"] == [1, 2] and sales["latestVersion"] == 2
    assert "prompt" not in sales  # metadata only
    assert [d["name"] for d in client.get(f"{BASE}?name=SUPPORT", headers=auth).json()["data"]] == [
        "support/a",
        "support/b",
    ]
    assert [
        d["name"] for d in client.get(f"{BASE}?label=staging", headers=auth).json()["data"]
    ] == ["support/a"]
    assert len(client.get(f"{BASE}?tag=cs", headers=auth).json()["data"]) == 2
    page2 = client.get(f"{BASE}?limit=2&page=2", headers=auth).json()
    assert [d["name"] for d in page2["data"]] == ["support/b"]
    assert page2["meta"]["totalPages"] == 2
    assert client.get(f"{BASE}?limit=0", headers=auth).status_code == 422


# --- project isolation -----------------------------------------------------------------------


def test_keys_only_see_their_own_project(client, auth, make_key, make_project, world):
    create(client, auth)
    other = make_project(world.org)
    _, public, secret = make_key(other)
    theirs = basic(public, secret)
    assert client.get(f"{BASE}/support", headers=theirs).status_code == 404
    assert client.get(BASE, headers=theirs).json()["data"] == []
    create(client, theirs, prompt="theirs")
    assert client.get(f"{BASE}/support", headers=auth).json()["prompt"] == "Hello {{name}}"


# --- key creation flag ------------------------------------------------------------------------


def test_admin_sets_the_flag_when_creating_a_key(client, login, world, db):
    login(client, world.users[Role.ADMIN])
    url = f"/project/{world.project.id}/settings/keys"
    assert client.post(url, data={"note": "plain"}).status_code == 201
    assert (
        client.post(url, data={"note": "ops", "may_move_protected_labels": "true"}).status_code
        == 201
    )
    flags = {k.note: k.may_move_protected_labels for k in db.scalars(select(ApiKey))}
    assert flags == {"plain": False, "ops": True}
    created = db.scalar(select(AuditLog).where(AuditLog.action == "api_key.created"))
    assert "may_move_protected_labels" in created.detail
    assert "can move protected labels" in client.get(url, headers={"Accept": "text/html"}).text


def test_member_cannot_create_keys(client, login, world):
    login(client, world.users[Role.MEMBER])
    url = f"/project/{world.project.id}/settings/keys"
    r = client.post(url, data={"note": "x", "may_move_protected_labels": "true"})
    assert r.status_code == 403


# --- limits and conflicts (review fixes) ------------------------------------------------------


def test_oversized_bodies_are_refused_before_parsing(client, auth):
    from traceloom.api.prompts_v2 import MAX_BODY_BYTES

    huge = "x" * (MAX_BODY_BYTES + 1)
    r = create(client, auth, prompt=huge)
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
    assert create(client, auth).status_code == 201
    r = client.patch(
        f"{BASE}/support/versions/1", json={"newLabels": ["a"] * MAX_BODY_BYTES}, headers=auth
    )
    assert r.status_code == 413


def test_malformed_json_is_still_a_422(client, auth):
    headers = {**auth, "content-type": "application/json"}
    r = client.post(BASE, content=b"{not json", headers=headers)
    assert r.status_code == 422


def test_a_save_conflict_is_a_409(client, auth, monkeypatch):
    from traceloom.prompts import service

    assert create(client, auth).status_code == 201
    monkeypatch.setattr(service, "_latest_version", lambda *a: 0)  # every attempt races to v1
    r = create(client, auth, prompt="second")
    assert r.status_code == 409 and "same time" in r.json()["error"]["message"]
