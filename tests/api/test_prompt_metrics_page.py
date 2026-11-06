from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceloom.auth.permissions import Role
from traceloom.db.models import Observation, Score, Trace
from traceloom.prompts import service
from traceloom.prompts.service import Actor

HTML = {"Accept": "text/html"}
XSS = "<script>alert(1)</script>"


def actor(world, role=Role.ADMIN):
    return Actor.for_user(world.users[role], role)


def save(db, world, name="support", content="Hello", **kw):
    service.create_version(
        db,
        world.project,
        actor(world),
        name=name,
        type="text",
        content=content,
        commit_message="m",
        **kw,
    )
    db.commit()


def url(world, name="support", suffix=""):
    return f"/project/{world.project.id}/prompts/{name}{suffix}"


def generation(
    db,
    world,
    obs_id,
    version,
    *,
    name="support",
    tokens=(100, 50),
    cost=0.0015,
    ms=1500,
    ago=timedelta(minutes=5),
    env="production",
    level="DEFAULT",
    model="claude-sonnet-5-5",
    project=None,
):
    project_id = (project or world.project).id
    start = datetime.now(UTC) - ago
    trace_id = f"tr-{obs_id}"
    db.add(Trace(project_id=project_id, id=trace_id, timestamp=start, environment=env))
    db.add(
        Observation(
            project_id=project_id,
            id=obs_id,
            trace_id=trace_id,
            type="generation",
            name="answer",
            start_time=start,
            end_time=start + timedelta(milliseconds=ms),
            model=model,
            usage_details={"input": tokens[0], "output": tokens[1], "total": sum(tokens)},
            cost_details={"total": cost},
            prompt_name=name,
            prompt_version=version,
            environment=env,
            level=level,
        )
    )
    db.commit()


@pytest.fixture
def admin(client, login, world):
    return login(client, world.users[Role.ADMIN])


@pytest.fixture
def support(db, world):
    save(db, world, content="one")
    save(db, world, content="two")


def test_tiles_reflect_the_selected_version(admin, db, world, support):
    generation(db, world, "a", 2, tokens=(100, 50), cost=0.002, ms=1000)
    generation(db, world, "b", 2, tokens=(200, 100), cost=0.004, ms=3000)
    generation(db, world, "c", 1, tokens=(5, 5), cost=0.5, ms=100)  # another version
    page = admin.get(url(world, suffix="/metrics?version=2"), headers=HTML)
    assert page.status_code == 200
    text = page.text
    assert "Linked generations" in text and "<b>2</b>" in text
    assert "450" in text  # 150 + 300 tokens
    assert "$0.01" in text  # 0.006 total cost at two places
    assert "2.00 s" in text  # median of 1 s and 3 s
    assert "tr-a" in text and "tr-b" in text and "tr-c" not in text


def test_all_versions_option(admin, db, world, support):
    generation(db, world, "a", 2)
    generation(db, world, "c", 1)
    page = admin.get(url(world, suffix="/metrics?version=all"), headers=HTML).text
    assert "tr-a" in page and "tr-c" in page and "<th>Version</th>" in page


def test_rows_link_to_trace_detail(admin, db, world, support):
    generation(db, world, "a", 2)
    page = admin.get(url(world, suffix="/metrics?version=2"), headers=HTML).text
    assert f'href="/project/{world.project.id}/traces/tr-a"' in page


def test_date_range_and_environment_filters(admin, db, world, support):
    generation(db, world, "new", 2, ago=timedelta(minutes=10))
    generation(db, world, "old", 2, ago=timedelta(days=3))
    generation(db, world, "stg", 2, env="staging")
    base = url(world, suffix="/metrics?version=2")
    day = admin.get(f"{base}&range=24h", headers=HTML).text
    assert "tr-new" in day and "tr-old" not in day
    week = admin.get(f"{base}&range=7d", headers=HTML).text
    assert "tr-old" in week
    prod = admin.get(f"{base}&range=7d&env=production", headers=HTML).text
    assert "tr-stg" not in prod and "tr-new" in prod
    assert "production" in prod  # the scope line names the environment
    everything = admin.get(f"{base}&range=7d&env=all", headers=HTML).text
    assert "tr-stg" in everything


def test_average_scores_on_linked_traces(admin, db, world, support):
    generation(db, world, "a", 2)
    db.add(
        Score(
            project_id=world.project.id,
            id="s1",
            trace_id="tr-a",
            name="helpfulness",
            value=0.5,
            timestamp=datetime.now(UTC),
        )
    )
    db.add(
        Score(
            project_id=world.project.id,
            id="s2",
            trace_id="tr-a",
            name="helpfulness",
            value=1.0,
            timestamp=datetime.now(UTC),
        )
    )
    db.commit()
    page = admin.get(url(world, suffix="/metrics?version=2"), headers=HTML).text
    assert "helpfulness" in page and "0.75" in page


def test_no_linked_generations_shows_the_empty_state(admin, world, support):
    page = admin.get(url(world, suffix="/metrics?version=2"), headers=HTML).text
    assert "No linked generations yet" in page and "start_generation" in page
    assert "Linked generations</div>" not in page  # no tiles


def test_unknown_version_and_prompt(admin, world, support):
    assert admin.get(url(world, suffix="/metrics?version=9"), headers=HTML).status_code == 404
    assert admin.get(url(world, suffix="/metrics?version=x"), headers=HTML).status_code == 400
    assert admin.get(url(world, "ghost", "/metrics"), headers=HTML).status_code == 404


def test_another_projects_prompt_is_404(admin, db, world, support, make_project):
    other = make_project(world.org)
    service.create_version(
        db,
        other,
        actor(world),
        name="theirs",
        type="text",
        content="x",
        commit_message="m",
    )
    db.commit()
    generation(db, world, "x1", 1, name="theirs", project=other)
    assert admin.get(url(world, "theirs", "/metrics"), headers=HTML).status_code == 404
    # and this project's metrics never include the other project's generations
    generation(db, world, "x2", 1, name="support", project=other)
    page = admin.get(url(world, suffix="/metrics?version=1"), headers=HTML).text
    assert "tr-x2" not in page


def test_viewer_can_read_metrics(client, login, db, world, support):
    generation(db, world, "a", 2)
    login(client, world.users[Role.VIEWER])
    assert client.get(url(world, suffix="/metrics?version=2"), headers=HTML).status_code == 200


def test_names_with_slashes(admin, db, world):
    save(db, world, name="folder/deep/prompt")
    generation(db, world, "a", 1, name="folder/deep/prompt")
    page = admin.get(url(world, "folder/deep/prompt", "/metrics?version=1"), headers=HTML)
    assert page.status_code == 200 and "tr-a" in page.text


def test_user_content_is_escaped(admin, db, world):
    save(db, world, name="x")
    generation(db, world, "a", 1, name="x", model=XSS)
    page = admin.get(url(world, "x", "/metrics?version=1"), headers=HTML).text
    assert XSS not in page and "&lt;script&gt;" in page


def test_the_detail_page_points_the_tab_at_the_partial(admin, db, world, support):
    page = admin.get(url(world, suffix="?tab=metrics&version=2&range=7d"), headers=HTML).text
    assert "/metrics?version=2&amp;range=7d" in page
    assert 'hx-trigger="load"' in page and 'aria-label="Date range"' in page
    plain = admin.get(url(world, suffix="?tab=metrics"), headers=HTML).text
    assert "range=24h" in plain
