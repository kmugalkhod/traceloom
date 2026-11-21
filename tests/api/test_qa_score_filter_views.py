"""QA T-10: `quality >= 0.5` filters the traces list, survives reload and saved views."""

from __future__ import annotations

import re
from html import unescape

from tests.api.test_trace_list_page import NOW, H, add, ids_in, token, url
from traceloom.auth.permissions import Role
from traceloom.ports import ScoreRow


def _scored(container, world):
    for tid, value in (("q-hi", 0.9), ("q-lo", 0.2)):
        score = ScoreRow(id=f"s-{tid}", trace_id=tid, name="quality", value=value, timestamp=NOW)
        add(container, world.project.id, tid, scores=[score])
    add(container, world.project.id, "q-none")


def test_score_filter_in_url_and_saved_view_round_trip(client, container, world, login):
    _scored(container, world)
    login(client, world.user(Role.MEMBER))
    query = "score=quality%3A%3E%3D%3A0.5"
    page = client.get(url(world, query), headers=H)
    assert ids_in(page.text) == ["q-hi"]
    assert ids_in(client.get(url(world, query), headers=H).text) == ["q-hi"]  # reload

    r = client.post(
        f"/project/{world.project.id}/views",
        data={"csrf_token": token(page.text), "name": "Good", "query": query},
        follow_redirects=False,
    )
    assert r.status_code == 303
    fresh = client.get(url(world), headers=H).text
    link = unescape(re.search(r'href="([^"]+)"[^>]*><span class="grow"><b>Good', fresh).group(1))
    assert "score=quality" in link
    assert ids_in(client.get(link, headers=H).text) == ["q-hi"]


def test_score_sort_puts_unscored_last(client, container, world, login):
    _scored(container, world)
    login(client, world.user(Role.MEMBER))
    ids = ids_in(client.get(url(world, "sort=score%3Aquality%3Adesc"), headers=H).text)
    assert ids.index("q-hi") < ids.index("q-lo") < ids.index("q-none")
