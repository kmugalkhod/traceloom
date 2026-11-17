"""`run_experiment`: run a task over every dataset item and score the outputs.

It never raises on a task or evaluator error: the error is recorded on that item and the run
goes on. Creating the run does raise `DatasetError`, since nothing can proceed without it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from traceloom_sdk.datasets import (
    DATASETS_PATH,
    RUN_ITEMS_PATH,
    Dataset,
    DatasetError,
    DatasetItem,
    request,
    resolve_client,
)

if TYPE_CHECKING:
    from traceloom_sdk.client import Traceloom

log = logging.getLogger("traceloom")

LINK_ATTEMPTS = 20  # the trace is ingested asynchronously; the link needs it to exist
LINK_WAIT_SECONDS = 0.25


@dataclass
class ItemResult:
    item_id: str
    trace_id: str = ""
    output: Any = None
    error: str | None = None
    scores: dict[str, Any] = field(default_factory=dict)
    evaluator_errors: dict[str, str] = field(default_factory=dict)
    linked: bool = False


@dataclass
class ExperimentResult:
    run_name: str
    run_id: str
    dataset_name: str
    url: str
    items: list[ItemResult] = field(default_factory=list)

    @property
    def outputs(self) -> dict[str, Any]:
        return {r.item_id: r.output for r in self.items}

    @property
    def scores(self) -> dict[str, dict[str, Any]]:
        return {r.item_id: r.scores for r in self.items}

    @property
    def errors(self) -> dict[str, str]:
        return {r.item_id: r.error for r in self.items if r.error}


def _evaluate(
    evaluator: Callable[..., Any], item: DatasetItem, output: Any
) -> list[tuple[str, Any, str | None]]:
    """Call an evaluator; normalise its return to (name, value, comment) triples."""
    result = evaluator(
        input=item.input,
        output=output,
        expected_output=item.expected_output,
        metadata=item.metadata,
    )
    default = getattr(evaluator, "__name__", "evaluator")
    triples = []
    for r in result if isinstance(result, list) else [result]:
        if isinstance(r, dict):
            triples.append((r.get("name", default), r["value"], r.get("comment")))
        elif r is not None:
            triples.append((default, r, None))
    return triples


def run_experiment(
    dataset: Dataset,
    task_fn: Callable[[DatasetItem], Any],
    evaluators: Sequence[Callable[..., Any]] = (),
    run_name: str | None = None,
    max_concurrency: int = 4,
    metadata: dict[str, Any] | None = None,
    description: str = "",
    client: Traceloom | None = None,
) -> ExperimentResult:
    """Run `task_fn(item)` for each item of `dataset` in a thread pool.

    Each call runs inside an `experiment:<run_name>` trace linked to its run item. Evaluators are
    called as `evaluator(input=, output=, expected_output=, metadata=)` and return a number,
    bool or str, a `{"name", "value", "comment"}` dict, or a list of those; each becomes a score
    carrying the run id. At most `max_concurrency` tasks run at once. Items that fail are not
    scored.
    """
    client = resolve_client(client or dataset._client)
    run_name = run_name or time.strftime("run-%Y%m%d-%H%M%S")
    run = request(
        client,
        "POST",
        f"{DATASETS_PATH}/{quote(dataset.name, safe='')}/runs",
        json={"name": run_name, "description": description, "metadata": metadata},
    )
    run_id = run["id"]
    result = ExperimentResult(
        run_name=run["name"],
        run_id=run_id,
        dataset_name=dataset.name,
        url=f"{client.host}/datasets/{run['datasetId']}?tab=runs&run={run_id}",
    )

    def work(item: DatasetItem) -> ItemResult:
        res = ItemResult(item.id)
        try:
            with client.start_span(f"experiment:{run_name}", input=item.input) as span:
                res.trace_id = span.trace_id
                try:
                    res.output = task_fn(item)
                    span.update(output=res.output)
                except Exception as exc:
                    res.error = f"{type(exc).__name__}: {exc}"
                    log.warning("Traceloom: task failed for item %s: %s", item.id, res.error)
                    raise  # lets the span record the error status
        except Exception:
            pass
        if not res.trace_id:
            res.error = res.error or "could not start a trace"
            return res
        res.linked = _link(client, run_id, item.id, res.trace_id, res.error)
        if res.error is None:
            for evaluator in evaluators:
                name = getattr(evaluator, "__name__", "evaluator")
                try:
                    for score_name, value, comment in _evaluate(evaluator, item, res.output):
                        res.scores[score_name] = value
                        client.score(
                            score_name,
                            value,
                            trace_id=res.trace_id,
                            comment=comment,
                            dataset_run_id=run_id,
                        )
                except Exception as exc:
                    res.evaluator_errors[name] = f"{type(exc).__name__}: {exc}"
                    log.warning("Traceloom: evaluator %s failed on %s: %s", name, item.id, exc)
        return res

    with ThreadPoolExecutor(max_workers=max(1, max_concurrency)) as pool:
        result.items = list(pool.map(work, dataset.items))
    client.flush()
    failed = sum(1 for r in result.items if r.error)
    status = "failed" if result.items and failed == len(result.items) else "completed"
    run_path = (
        f"{DATASETS_PATH}/{quote(dataset.name, safe='')}/runs/{quote(result.run_name, safe='')}"
    )
    try:
        request(client, "PATCH", run_path, json={"status": status})
    except DatasetError as exc:
        log.warning("Traceloom: could not finish run %s: %s", run_name, exc)
    return result


def _link(
    client: Traceloom, run_id: str, item_id: str, trace_id: str, error: str | None = None
) -> bool:
    """Link the item to its trace; retried while the trace is still being ingested."""
    client.flush()
    body: dict[str, Any] = {"runId": run_id, "datasetItemId": item_id, "traceId": trace_id}
    if error:
        body.update(status="failed", error=error)
    for attempt in range(LINK_ATTEMPTS):
        try:
            request(client, "POST", RUN_ITEMS_PATH, json=body)
            return True
        except DatasetError as exc:
            if exc.status != 404 or attempt == LINK_ATTEMPTS - 1:
                log.warning("Traceloom: could not link item %s: %s", item_id, exc)
                return False
            time.sleep(LINK_WAIT_SECONDS)
    return False
