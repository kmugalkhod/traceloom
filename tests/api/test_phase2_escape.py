"""AC-9 for prompts: user content in every field renders escaped on every prompt view."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceloom.auth.permissions import Role
from traceloom.db.models import Observation, Trace
from traceloom.prompts import service
from traceloom.prompts.service import Actor

HTML = {"Accept": "text/html"}
SCRIPT = "<script>alert(1)</script>"
IMG = "<img src=x onerror=alert(1)>"
PAYLOAD = f"{SCRIPT}{IMG}"
RAW = ("<script>alert", "<img src=x", "onerror=alert(1)>")


def assert_escaped(html: str, where: str) -> None:
    for raw in RAW:
        assert raw not in html, f"{where}: unescaped {raw!r}"
    assert "&lt;script&gt;" in html or "&lt;img" in html, f"{where}: payload not shown as text"


def only_known_scripts(html: str, where: str) -> None:
    """The only <script> tags are our own static files (no inline code)."""
    import re

    for tag in re.findall(r"<script\b[^>]*>", html):
        assert 'src="/static/' in tag, f"{where}: unexpected script tag {tag}"


@pytest.fixture
def seeded(db, world):
    who = Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)
    project = world.project
    # A user whose display name is a payload shows up as author and in label history.
    world.users[Role.ADMIN].name = PAYLOAD
    db.commit()
    service.create_version(
        db,
        project,
        who,
        name="shared/tone",
        type="text",
        content=f"tone {PAYLOAD}",
        commit_message="tone",
        labels=["live"],
    )
    for n in (1, 2):
        service.create_version(
            db,
            project,
            who,
            name="probe/escape",
            type="text",
            content=f"v{n} {PAYLOAD} {{{{who}}}} @@@prompt:shared/tone|label=live@@@",
            config={"note": PAYLOAD, PAYLOAD: 1},
            commit_message=f"commit {PAYLOAD}",
            tags=[SCRIPT],
            labels=["production"] if n == 1 else ["staging"],
        )
    service.create_version(
        db,
        project,
        who,
        name="probe/chat",
        type="chat",
        content=[{"role": "user", "content": PAYLOAD}, {"role": "placeholder", "content": PAYLOAD}],
        commit_message="chat",
    )
    start = datetime.now(UTC) - timedelta(minutes=3)
    db.add(Trace(project_id=project.id, id="tr-1", timestamp=start))
    db.add(
        Observation(
            project_id=project.id,
            id="ob-1",
            trace_id="tr-1",
            type="generation",
            start_time=start,
            end_time=start + timedelta(seconds=1),
            model=PAYLOAD,
            level="ERROR",
            prompt_name="probe/escape",
            prompt_version=2,
            usage_details={"total": 5},
        )
    )
    db.commit()


def pages(world):
    root = f"/project/{world.project.id}/prompts"
    detail = f"{root}/probe/escape"
    return {
        "list": f"{root}",
        "list search": f"{root}?q=alert",
        "list search echo": f"{root}?q={PAYLOAD}",
        "list tag echo": f"{root}?tag={PAYLOAD}",
        "list label echo": f"{root}?label={PAYLOAD}",
        "list folder echo": f"{root}?folder={PAYLOAD}",
        "list folder": f"{root}?folder=probe",
        "detail": detail,
        "detail v1": f"{detail}?version=1",
        "detail older": f"{detail}?older=1",
        "detail toast echo": f"{detail}?moved={PAYLOAD}&was=1&now=2",
        "detail error echo": f"{detail}?err={PAYLOAD}",
        "compare": f"{detail}?tab=compare&from=1&to=2",
        "preview": f"{detail}?tab=preview&version=2&v_who={PAYLOAD}",
        "preview unfilled": f"{detail}?tab=preview",
        "metrics tab": f"{detail}?tab=metrics&version=2",
        "metrics partial": f"{detail}/metrics?version=2&range=7d",
        "metrics partial env echo": f"{detail}/metrics?version=2&env={PAYLOAD}",
        "chat detail": f"{root}/probe/chat",
        "chat compare": f"{root}/probe/chat?tab=compare&from=1&to=1",
        "chat preview": f"{root}/probe/chat?tab=preview",
        "editor": f"{root}/new",
        "editor version": f"{root}/new?from=probe/escape",
        "editor chat version": f"{root}/new?from=probe/chat",
    }


def test_every_prompt_view_escapes_user_content(client, login, world, seeded):
    login(client, world.users[Role.ADMIN])
    problems = []
    for where, url in pages(world).items():
        r = client.get(url, headers=HTML)
        if r.status_code == 404:  # an echoed bogus folder or similar
            continue
        assert r.status_code == 200, f"{where}: {r.status_code}"
        try:
            only_known_scripts(r.text, where)
            for raw in RAW:
                assert raw not in r.text, f"{where}: unescaped {raw!r}"
        except AssertionError as exc:
            problems.append(str(exc))
    assert not problems, problems


def test_pages_that_show_stored_content_show_it_as_text(client, login, world, seeded):
    login(client, world.users[Role.ADMIN])
    shown = {
        "detail": f"/project/{world.project.id}/prompts/probe/escape",
        "compare": f"/project/{world.project.id}/prompts/probe/escape?tab=compare&from=1&to=2",
        "preview": f"/project/{world.project.id}/prompts/probe/escape?tab=preview&v_who={PAYLOAD}",
        "metrics": f"/project/{world.project.id}/prompts/probe/escape/metrics?version=2",
        "list": f"/project/{world.project.id}/prompts?q=alert",
        "chat": f"/project/{world.project.id}/prompts/probe/chat",
        "editor": f"/project/{world.project.id}/prompts/new?from=probe/escape",
    }
    for where, url in shown.items():
        assert_escaped(client.get(url, headers=HTML).text, where)


def test_invalid_submissions_echo_escaped(client, login, world, seeded):
    import re

    login(client, world.users[Role.MEMBER])
    page = client.get(f"/project/{world.project.id}/prompts/new", headers=HTML).text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    data = {
        "csrf_token": token, "mode": "prompt", "type": "text", "name": f"bad {PAYLOAD}",
        "text": PAYLOAD, "tags": PAYLOAD, "config": PAYLOAD, "commit_message": "",
        "cfg_model": PAYLOAD, "other_labels": PAYLOAD,
    }  # fmt: skip
    r = client.post(f"/project/{world.project.id}/prompts/new", data=data, headers=HTML)
    assert r.status_code == 400
    assert_escaped(r.text, "invalid submission")
    for raw in RAW:
        assert raw not in r.text


def test_label_move_with_a_hostile_label_is_rejected_and_not_echoed_raw(
    client, login, world, seeded
):
    import re

    login(client, world.users[Role.ADMIN])
    base = f"/project/{world.project.id}/prompts/probe/escape"
    page = client.get(base, headers=HTML).text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    r = client.post(
        f"{base}/labels",
        data={"csrf_token": token, "label": PAYLOAD, "version": "1"},
        headers=HTML,
        follow_redirects=True,
    )
    assert r.status_code == 200 and "Labels use lowercase" in r.text
    for raw in RAW:
        assert raw not in r.text
