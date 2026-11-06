"""NFR-1 (lite): 1k prompts x 20 versions and 50k linked generations.

- GET /api/public/v2/prompts/{name}?label=production: p95 < 50 ms on a warm server
- the prompts list page: < 500 ms
- per-version metrics: < 1 s
"""

from __future__ import annotations

import base64
import random
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from traceloom.auth.api_keys import generate_key_pair, hash_secret
from traceloom.auth.csrf import COOKIE_NAME as CSRF_COOKIE
from traceloom.auth.passwords import hash_password
from traceloom.auth.sessions import COOKIE_NAME as SESSION_COOKIE
from traceloom.auth.sessions import issue_session
from traceloom.config import Settings
from traceloom.db.models import (
    ApiKey,
    Membership,
    Observation,
    Organization,
    Project,
    Prompt,
    PromptLabel,
    PromptVersion,
    Trace,
    User,
)
from traceloom.main import create_app

pytestmark = pytest.mark.perf

PROMPTS, VERSIONS, GENERATIONS = 1_000, 20, 50_000
HOT = "perf/hot-prompt"  # carries half of the generations, so its metrics are the heavy case
API_P95, LIST_PAGE, METRICS = 0.050, 0.5, 1.0
SAMPLES = 300


def name_of(i: int) -> str:
    return f"team-{i % 25:02}/prompt-{i:04}"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("prompt-perf")
    settings = Settings(
        TRACELOOM_ENV="test",
        TRACELOOM_MODE="lite",
        DATABASE_URL=f"sqlite:///{root / 'perf.db'}",
        TRACELOOM_BLOB_DIR=str(root / "blobs"),
        TRACELOOM_SERVER_SECRET="perf-secret-not-for-production",
    )
    app = create_app(settings)
    rng = random.Random(7)
    now = datetime.now(UTC)
    public, secret = generate_key_pair()
    with TestClient(app) as client:
        container = app.state.container
        with container.session_factory.begin() as s:
            user = User(
                email="perf@example.com", name="Perf", password_hash=hash_password("x" * 12)
            )
            org = Organization(name="Perf Org")
            s.add_all([user, org])
            s.flush()
            project = Project(org_id=org.id, name="perf")
            s.add_all([Membership(user_id=user.id, org_id=org.id, role="admin"), project])
            s.flush()
            s.add(
                ApiKey(
                    project_id=project.id,
                    public_key=public,
                    secret_hash=hash_secret(secret, settings.secret_key),
                )
            )
            prompts, versions, labels = [], [], []
            names = [name_of(i) for i in range(PROMPTS - 1)] + [HOT]
            for i, name in enumerate(names):
                pid = f"p{i:05}"
                prompts.append(
                    dict(
                        id=pid,
                        project_id=project.id,
                        name=name,
                        type="text",
                        tags=["perf", f"t{i % 10}"],
                        created_at=now,
                        updated_at=now - timedelta(minutes=i),
                    )
                )
                for v in range(1, VERSIONS + 1):
                    versions.append(
                        dict(
                            id=f"v{i:05}-{v:02}",
                            project_id=project.id,
                            prompt_id=pid,
                            version=v,
                            content=f"Version {v} of {name}. Answer {{{{question}}}} politely. "
                            + "Be concise. " * 20,
                            config={"model": "claude-sonnet-5-5", "temperature": 0.2},
                            commit_message=f"change {v}",
                            created_at=now - timedelta(days=VERSIONS - v),
                            dependencies=[],
                        )
                    )
                labels.append(
                    dict(
                        project_id=project.id,
                        prompt_id=pid,
                        label="production",
                        version=VERSIONS - 1,
                        updated_at=now,
                    )
                )
            s.bulk_insert_mappings(Prompt, prompts)
            s.bulk_insert_mappings(PromptVersion, versions)
            s.bulk_insert_mappings(PromptLabel, labels)

            traces, observations = [], []
            for g in range(GENERATIONS):
                hot = g % 2 == 0
                name = HOT if hot else names[rng.randrange(PROMPTS - 1)]
                start = now - timedelta(seconds=rng.randrange(0, 20 * 3600))
                tin, tout = rng.randrange(50, 2000), rng.randrange(10, 600)
                traces.append(
                    dict(
                        project_id=project.id, id=f"g{g}", timestamp=start, environment="production"
                    )
                )
                observations.append(
                    dict(
                        project_id=project.id,
                        id=f"o{g}",
                        trace_id=f"g{g}",
                        type="generation",
                        name="answer",
                        start_time=start,
                        end_time=start + timedelta(milliseconds=rng.randrange(200, 5000)),
                        model="claude-sonnet-5-5",
                        usage_details={"input": tin, "output": tout, "total": tin + tout},
                        cost_details={"total": (tin * 3 + tout * 15) * 1e-6},
                        environment="production",
                        metadata_={},
                        model_parameters={},
                        prompt_name=name,
                        prompt_version=3 if hot else rng.randrange(1, VERSIONS + 1),
                    )
                )
            s.bulk_insert_mappings(Trace, traces)
            s.bulk_insert_mappings(Observation, observations)
            project_id, user_id, version = project.id, user.id, user.session_version

        client.cookies.set(SESSION_COOKIE, issue_session(settings, user_id, version))
        client.get("/api/public/health")
        client.headers["X-CSRF-Token"] = client.cookies[CSRF_COOKIE]
        auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
        yield {
            "client": client,
            "project": project_id,
            "basic": {"Authorization": f"Basic {auth}"},
            "names": names,
        }


def p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[int(len(ordered) * 0.95) - 1]


def test_seed_has_the_expected_volume(env):
    client, project = env["client"], env["project"]
    page = client.get("/api/public/v2/prompts?limit=200", headers=env["basic"]).json()
    assert page["meta"]["totalItems"] == PROMPTS
    got = client.get(f"/api/public/v2/prompts/{HOT}?version=20", headers=env["basic"]).json()
    assert got["version"] == 20 and project


def test_get_production_prompt_p95_under_50ms(env):
    client, rng = env["client"], random.Random(3)
    path = "/api/public/v2/prompts/"
    for name in env["names"][:20]:  # warm: key cache, connections, query plans
        assert client.get(f"{path}{name}?label=production", headers=env["basic"]).status_code == 200
    samples = []
    for _ in range(SAMPLES):
        name = env["names"][rng.randrange(PROMPTS)]
        start = time.perf_counter()
        r = client.get(f"{path}{name}?label=production", headers=env["basic"])
        samples.append(time.perf_counter() - start)
        assert r.status_code == 200 and r.json()["version"] == VERSIONS - 1
    assert p95(samples) < API_P95, f"p95 {p95(samples) * 1000:.1f} ms"


def test_prompts_list_page_under_500ms(env):
    client = env["client"]
    url = f"/project/{env['project']}/prompts"
    client.get(url, headers={"Accept": "text/html"})  # warm
    for query in ("", "?q=prompt-04", "?tag=t3", "?label=production&sort=name", "?folder=team-07"):
        start = time.perf_counter()
        r = client.get(url + query, headers={"Accept": "text/html"})
        took = time.perf_counter() - start
        assert r.status_code == 200
        assert took < LIST_PAGE, f"{query or 'first page'}: {took:.3f}s"


def test_per_version_metrics_under_a_second(env):
    client = env["client"]
    base = f"/project/{env['project']}/prompts/{HOT}/metrics"
    client.get(f"{base}?version=3&range=24h", headers={"Accept": "text/html"})  # warm
    for query in (
        "version=3&range=24h",
        "version=all&range=24h",
        "version=3&range=30d&env=production",
    ):
        start = time.perf_counter()
        r = client.get(f"{base}?{query}", headers={"Accept": "text/html"})
        took = time.perf_counter() - start
        assert r.status_code == 200 and "Linked generations" in r.text
        assert took < METRICS, f"{query}: {took:.3f}s"
    heavy = client.get(f"{base}?version=3&range=24h", headers={"Accept": "text/html"}).text
    assert "25,000" in heavy  # every even generation is linked to the hot prompt v3
