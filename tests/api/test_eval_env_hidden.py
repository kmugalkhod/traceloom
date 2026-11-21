from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.helpers_web import seed_trace
from traceloom.auth.permissions import Role


def test_lists_hide_eval_and_experiment_environments_unless_selected(
    client, login, world, container
):
    pid = world.project.id
    recent = datetime.now(UTC) - timedelta(minutes=10)
    seed_trace(
        container, pid, "t-prod", name="prod-trace", environment="production", timestamp=recent
    )
    seed_trace(
        container,
        pid,
        "t-judge",
        name="judge-trace",
        environment="traceloom-eval",
        timestamp=recent,
    )
    seed_trace(
        container,
        pid,
        "t-exp",
        name="exp-trace",
        environment="traceloom-experiment",
        timestamp=recent,
    )
    login(client, world.users[Role.MEMBER])
    page = client.get(f"/project/{pid}/traces?env=all").text
    assert "prod-trace" in page and "judge-trace" not in page and "exp-trace" not in page
    judge = client.get(f"/project/{pid}/traces?env=traceloom-eval").text
    assert "judge-trace" in judge and "prod-trace" not in judge
