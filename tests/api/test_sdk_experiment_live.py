"""QA T-17: the real SDK (run_experiment) against the real app, in-process."""

from __future__ import annotations

import threading
import time

import httpx
import pytest
from traceloom_sdk import Traceloom, create_item, get_dataset, run_experiment, set_default_client
from traceloom_sdk.datasets import create_dataset


class Bridge(httpx.BaseTransport):
    """Routes SDK requests into the app's TestClient (adapts the response stream type)."""

    def __init__(self, client):
        self.client = client

    def handle_request(self, request):
        r = self.client.request(
            request.method, str(request.url), content=request.read(), headers=request.headers
        )
        return httpx.Response(r.status_code, headers=r.headers, content=r.content)


@pytest.fixture
def sdk(client, world, make_key):
    _, public, secret = make_key(world.project)
    c = Traceloom(
        host="http://testserver",
        public_key=public,
        secret_key=secret,
        flush_interval=60,
        transport=Bridge(client),
    )
    yield c
    c.shutdown(2)
    set_default_client(None)


def auth_of(sdk):
    import base64

    token = base64.b64encode(f"{sdk.public_key}:{sdk.secret_key}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_experiment_end_to_end(client, container, sdk):
    ds = create_dataset("qa17", client=sdk)
    ds = get_dataset("qa17", client=sdk)
    for n in range(3):
        create_item(ds, input={"q": n}, expected_output=n * 2)
    ds = get_dataset("qa17", client=sdk)
    assert len(ds.items) == 3

    def task(item):
        if item.input["q"] == 1:
            raise ValueError("boom")
        return item.input["q"] * 2

    def exact(*, input, output, expected_output, metadata):
        return 1.0 if output == expected_output else 0.0

    result = run_experiment(ds, task, [exact], run_name="live-run", metadata={"k": "v"})

    # result: outputs, scores, errors, url
    assert len(result.errors) == 1 and "boom" in next(iter(result.errors.values()))
    assert sorted(v for v in result.outputs.values() if v is not None) == [0, 4]
    assert sum(1 for s in result.scores.values() if s == {"exact": 1.0}) == 2
    assert result.url.startswith("http://testserver/datasets/") and result.run_id in result.url
    assert all(r.linked for r in result.items)  # the failed item is linked too

    h = auth_of(sdk)
    items = client.get("/api/public/datasets/qa17/runs/live-run", headers=h)
    print(items.status_code, items.text[:2000])
    assert items.status_code == 200
    body = items.json()
    assert body["status"] == "completed"
    failed = [ri for ri in body["runItems"] if ri["status"] == "failed"]
    assert len(failed) == 1 and "boom" in failed[0]["error"]
    assert sum(1 for ri in body["runItems"] if ri["status"] == "completed") == 2
    scores = client.get("/api/public/scores", headers=h)
    print(scores.status_code, scores.text[:2000])
    assert scores.status_code == 200
    data = scores.json()["data"]
    assert len(data) == 2 and all(s.get("datasetRunId") == result.run_id for s in data)
    from sqlalchemy import select

    from traceloom.db.models import Trace

    with container.session_factory() as db:
        for r in result.items:
            trace = db.scalar(select(Trace).where(Trace.id == r.trace_id))
            assert trace is not None and trace.name == "experiment:live-run"
    assert all(r.trace_id in items.text for r in result.items)


def test_concurrency_limit_live(client, sdk):
    create_dataset("conc", client=sdk)
    ds = get_dataset("conc", client=sdk)
    for n in range(6):
        create_item(ds, input=n)
    ds = get_dataset("conc", client=sdk)
    lock, state = threading.Lock(), {"now": 0, "peak": 0}

    def task(item):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1

    run_experiment(ds, task, max_concurrency=2)
    assert state["peak"] == 2
