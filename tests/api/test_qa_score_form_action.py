"""QA T-10: config-driven score forms on the full trace page must post to the trace's URL."""

from __future__ import annotations

import re

import pytest

from tests.helpers_web import seed_trace
from traceloom.auth.permissions import Role


@pytest.fixture
def trace(container, world):
    seed_trace(container, world.project.id, "t1")


def test_config_input_forms_post_to_the_trace_scores_url(
    client, login, world, trace, make_score_config
):
    make_score_config(world.project, "quality")
    login(client, world.user(Role.MEMBER), csrf=False)
    base = f"/project/{world.project.id}/traces/t1"
    html = client.get(base + "/discussion").text
    tags = re.findall(r"<form[^>]*data-score-form[^>]*>", html)
    assert tags
    for tag in tags:
        assert f'action="{base}/scores"' in tag, tag
        assert f'hx-post="{base}/scores"' in tag, tag
