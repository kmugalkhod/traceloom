"""The compare panel fragment: usable connections as data, at most 4 variants, Member+."""

from __future__ import annotations

import json
import re

from traceloom.auth.permissions import Role
from traceloom.llm import connections
from traceloom.llm.connections import ConnectionPolicy
from traceloom.prompts.service import Actor


def url(world):
    return f"/project/{world.project.id}/playground/compare-panel"


def test_panel_lists_usable_connections_without_secrets(client, login, world, db, container):
    admin = Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)
    connections.create(
        db, world.project, admin, ConnectionPolicy.from_settings(container.settings),
        provider="anthropic", name="</script><b>x", secrets={"api_key": "sk-ant-secret-777777"},
        custom_models=["mine"],
    )  # fmt: skip
    db.commit()
    member = login(client, world.users[Role.MEMBER])
    html = member.get(url(world), headers={"Accept": "text/html"}).text
    assert 'data-max="4"' in html and 'id="variant-tpl"' in html and "data-run-all" in html
    raw = re.search(r'id="compare-data">(.*?)</script>', html, re.S).group(1)
    data = json.loads(raw)  # the name's </script> cannot end the data block
    assert data[0]["name"] == "</script><b>x" and "mine" in data[0]["models"]
    assert "sk-ant" not in html and "777777" not in html


def test_panel_without_connections_says_so(client, login, world):
    member = login(client, world.users[Role.MEMBER])
    html = member.get(url(world), headers={"Accept": "text/html"}).text
    assert "Add an LLM connection" in html


def test_viewer_cannot_open_the_panel(client, login, world):
    viewer = login(client, world.users[Role.VIEWER])
    assert viewer.get(url(world)).status_code == 403
