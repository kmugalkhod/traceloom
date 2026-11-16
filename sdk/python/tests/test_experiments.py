import json
import threading
import time

import httpx
import pytest

from traceloom_sdk import (
    DatasetError,
    create_dataset,
    create_item,
    get_dataset,
    run_experiment,
)


class Api:
    """A fake Traceloom server: ingestion (recorded) plus the public datasets API."""

    def __init__(self, n_items=3):
        self.events = []
        self.links = []
        self.runs = []
        self.items = [
            {"id": f"i{n}", "input": {"q": n}, "expectedOutput": n * 2, "metadata": None}
            for n in range(n_items)
        ]
        self.link_404s = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        if path == "/api/public/ingestion":
            self.events.extend(body["batch"])
            return httpx.Response(207, json={"successes": [], "errors": []})
        if path == "/api/public/datasets" and request.method == "POST":
            return httpx.Response(201, json={"id": "d1", "name": body["name"]})
        if path == "/api/public/datasets/qa":
            return httpx.Response(200, json={"id": "d1", "name": "qa", "description": "x"})
        if path == "/api/public/datasets/qa/items" and request.method == "GET":
            page = int(request.url.params["page"])
            return httpx.Response(200, json={"data": self.items if page == 1 else []})
        if path == "/api/public/datasets/qa/items":
            return httpx.Response(201, json={"id": "new", **body})
        if path == "/api/public/datasets/qa/runs":
            self.runs.append(body)
            return httpx.Response(201, json={"id": "r1", "name": body["name"], "datasetId": "d1"})
        if path == "/api/public/dataset-run-items":
            if self.link_404s:
                self.link_404s -= 1
                return httpx.Response(404, json={})
            self.links.append(body)
            return httpx.Response(201, json={"id": "ri"})
        return httpx.Response(404, json={})

    def scores(self):
        return [e["body"] for e in self.events if e["type"] == "score-create"]


@pytest.fixture
def api():
    return Api()


@pytest.fixture
def sdk(api, make_client, recorder):
    return make_client(transport=httpx.MockTransport(api))


def test_get_dataset_and_create_helpers(api, sdk):
    ds = get_dataset("qa", client=sdk)
    assert [i.id for i in ds.items] == ["i0", "i1", "i2"]
    assert ds.items[1].expected_output == 2
    assert create_dataset("fresh", client=sdk).id == "d1"
    item = create_item(ds, input="a", expected_output="b")
    assert item.input == "a" and len(ds.items) == 4


def test_dataset_error_raised(api, sdk):
    with pytest.raises(DatasetError):
        get_dataset("missing", client=sdk)


def test_run_experiment_links_and_scores(api, sdk):
    ds = get_dataset("qa", client=sdk)

    def exact(*, input, output, expected_output, metadata):
        return 1.0 if output == expected_output else 0.0

    result = run_experiment(
        ds, lambda item: item.input["q"] * 2, [exact], run_name="v1", metadata={"m": 1}
    )
    assert api.runs == [{"name": "v1", "description": "", "metadata": {"m": 1}}]
    assert sorted(link["datasetItemId"] for link in api.links) == ["i0", "i1", "i2"]
    named = [e["body"] for e in api.events if e["type"] == "trace-create" and e["body"].get("name")]
    assert {t["name"] for t in named} == {"experiment:v1"}
    assert {link["traceId"] for link in api.links} == {t["id"] for t in named}
    assert all(link["runId"] == "r1" for link in api.links)
    scores = api.scores()
    assert len(scores) == 3
    assert all(
        s["datasetRunId"] == "r1" and s["name"] == "exact" and s["value"] == 1.0 for s in scores
    )
    assert result.outputs == {"i0": 0, "i1": 2, "i2": 4}
    assert result.scores["i1"] == {"exact": 1.0}
    assert result.errors == {}
    assert result.run_id == "r1" and "r1" in result.url


def test_task_error_recorded_not_raised(api, sdk):
    ds = get_dataset("qa", client=sdk)

    def task(item):
        if item.id == "i1":
            raise ValueError("boom")
        return item.id

    result = run_experiment(ds, task, [lambda **kw: 1], run_name="v2")
    assert list(result.errors) == ["i1"] and "boom" in result.errors["i1"]
    assert result.outputs["i0"] == "i0" and result.outputs["i2"] == "i2"
    assert len(api.links) == 3  # the failed item is linked to its trace too
    assert len(api.scores()) == 2  # but not scored
    errored = [
        e["body"] for e in api.events if e["type"] == "span-update" and e["body"].get("level")
    ]
    assert any(b["level"] == "ERROR" for b in errored)


def test_evaluator_error_does_not_stop_run(api, sdk):
    ds = get_dataset("qa", client=sdk)

    def bad(**kw):
        raise RuntimeError("nope")

    result = run_experiment(ds, lambda i: 1, [bad, lambda **kw: {"name": "ok", "value": 1}])
    assert result.errors == {}
    assert result.items[0].evaluator_errors["bad"].startswith("RuntimeError")
    assert len(api.scores()) == 3


def test_link_retried_until_trace_exists(api, sdk, monkeypatch):
    monkeypatch.setattr("traceloom_sdk.experiments.LINK_WAIT_SECONDS", 0.001)
    api.link_404s = 2
    ds = get_dataset("qa", client=sdk)
    ds.items = ds.items[:1]
    result = run_experiment(ds, lambda i: 1)
    assert result.items[0].linked and len(api.links) == 1


@pytest.mark.parametrize("limit", [1, 2])
def test_max_concurrency_respected(api, sdk, limit):
    api.items = [{"id": f"i{n}", "input": n} for n in range(6)]
    ds = get_dataset("qa", client=sdk)
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def task(item):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.03)
        with lock:
            state["now"] -= 1

    run_experiment(ds, task, max_concurrency=limit)
    assert state["peak"] == limit
