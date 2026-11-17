"""Experiments from the UI: the New experiment dialog and the run detail page with live progress."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.auth.deps import Access, form_data, require
from traceloom.auth.permissions import Perm
from traceloom.datasets import experiment, service
from traceloom.datasets import runs as dataset_runs
from traceloom.datasets.service import DatasetError, DatasetNotFound, NotAllowed
from traceloom.db.models import Score, User
from traceloom.db.models_datasets import Dataset, DatasetRun, DatasetRunItem
from traceloom.db.models_eval import EvalExecution, EvalTemplate, EvalTemplateVersion
from traceloom.db.session import get_db
from traceloom.evals import spend, templates
from traceloom.evals.mapping import IMPLICIT_SOURCES
from traceloom.llm import connections
from traceloom.llm.connections import ConnectionPolicy
from traceloom.prompts import service as prompt_service
from traceloom.prompts.variables import find_message_variables, find_variables
from traceloom.web.context import page_context
from traceloom.web.routers.datasets import actor_of, base_url, load_dataset
from traceloom.web.templating import render

router = APIRouter(tags=["dataset-runs"])

ITEM_LIMIT = 200
POLL_SECONDS = 2
FINISHED = ("succeeded", "failed", "capped", "completed")
STATUS_ORDER = ("succeeded", "failed", "capped", "running", "pending")


@dataclass
class ExperimentForm:
    """The dialog's values, an error to show, and whether to reopen the dialog."""

    values: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    field_errors: dict[str, str] = field(default_factory=dict)


def _prompt_options(db: Session, access: Access) -> list[dict[str, Any]]:
    actor = prompt_service.Actor(
        "user", access.user.id, access.role, getattr(access.user, "name", None)
    )
    out = []
    for summary in prompt_service.list_prompts(db, access.project, actor, limit=200).items:
        try:
            view = prompt_service.get(db, access.project, actor, summary.name, resolve=False)
        except (prompt_service.PromptError, prompt_service.PromptNotFound):
            continue
        variables = (
            find_variables(view.prompt)
            if isinstance(view.prompt, str)
            else find_message_variables(view.prompt)
        )
        out.append(
            {
                "name": summary.name,
                "latest": summary.latest_version,
                "labels": sorted(summary.labels),
                "variables": list(variables),
            }
        )
    return out


def dialog_context(
    request: Request,
    db: Session,
    access: Access,
    dataset: Dataset,
    form: ExperimentForm | None = None,
) -> dict[str, Any]:
    """Everything the New experiment dialog needs; empty for roles without the permission."""
    actor = actor_of(access)
    if not actor.can(Perm.EXPERIMENTS_RUN):
        return {"experiment": None}
    prompts = _prompt_options(db, access)
    container = request.app.state.container
    policy = ConnectionPolicy.from_settings(container.settings)
    usable = connections.list_usable(db, access.project, actor, policy)
    evaluators = [
        {
            "id": v.latest.id,
            "name": v.template.name,
            "version": v.latest.version,
            "builtin": v.builtin,
            "variables": [x for x in v.variables if x not in IMPLICIT_SOURCES],
        }
        for v in templates.list_templates(db, access.project.id)
    ]
    status = spend.status(db, access.project.id, container.settings)
    spec = {
        "prompts": prompts,
        "connections": [{"id": c.id, "models": list(c.models)} for c in usable],
    }
    form = form or ExperimentForm()
    return {
        "experiment": {
            "prompts": prompts,
            "connections": usable,
            "evaluators": evaluators,
            "spec": json.dumps(spec),
            "cap_reached": status.reached,
            "form": form,
            "values": form.values,
        }
    }


def _form_values(form: FormData) -> dict[str, Any]:
    values: dict[str, Any] = {
        k: str(form.get(k, "")).strip() for k in ("name", "prompt", "ref", "connection_id", "model")
    }
    values["evaluators"] = [str(v) for v in form.getlist("evaluator")]
    values["mapping"] = {
        k[len("map_") :]: str(v).strip() for k, v in form.multi_items() if k.startswith("map_")
    }
    return values


def _mapping_spec(text: str, variable: str) -> dict[str, str]:
    """`input.question` -> {source: input, path: question}; `expected_output` -> whole value."""
    text = text or f"input.{variable}"
    source, _, path = text.partition(".")
    spec = {"source": source}
    if path:
        spec["path"] = path if path.startswith("$") else f"$.{path}"
    return spec


def _parse(db: Session, values: dict[str, Any]) -> dict[str, Any]:
    if not values["name"]:
        raise DatasetError("Give the run a name.")
    if not values["prompt"]:
        raise DatasetError("Pick a prompt.")
    kind, _, ref = values["ref"].partition(":")
    kwargs: dict[str, Any] = {"prompt_name": values["prompt"]}
    if kind == "v" and ref.isdigit():
        kwargs["prompt_version"] = int(ref)
    elif kind == "l" and ref:
        kwargs["prompt_label"] = ref
    else:
        raise DatasetError("Pick a prompt version or label.")
    chosen = []
    for version_id in values["evaluators"]:
        version = db.get(EvalTemplateVersion, version_id)
        if version is None:
            raise DatasetError("An evaluator template no longer exists.")
        names = [v for v in find_variables(version.prompt) if v not in IMPLICIT_SOURCES]
        chosen.append(
            {
                "template_version_id": version.id,
                "score_name": _score_name(db, version),
                "mapping": {n: {"source": "input", "path": f"$.{n}"} for n in names},
            }
        )
    kwargs["evaluators"] = chosen
    mapping = {var: _mapping_spec(text, var) for var, text in (values.get("mapping") or {}).items()}
    kwargs["mapping"] = mapping
    return kwargs


def _score_name(db: Session, version: EvalTemplateVersion) -> str:
    template = db.get(EvalTemplate, version.template_id)
    return (template.name if template else "experiment")[:200]


def _reopen(request, db, access, dataset, form: ExperimentForm, status: int):
    from traceloom.web.routers.datasets import render_detail

    return render_detail(request, db, access, dataset, tab="runs", experiment_form=form,
                         http_status=status)  # fmt: skip


@router.post("/project/{project_id}/datasets/{dataset_id}/experiments", response_class=HTMLResponse)
def start_experiment(
    request: Request,
    dataset_id: str,
    access: Access = Depends(require(Perm.EXPERIMENTS_RUN)),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    dataset = load_dataset(db, access, dataset_id)
    values = _form_values(form)
    try:
        kwargs = _parse(db, values)
        existing = dataset_runs.find_run(db, access.project, dataset, values["name"])
        if existing is not None:
            raise DatasetError(
                f'A run named "{values["name"]}" already exists. Try "{values["name"]} (2)".'
            )
        run = experiment.start_experiment(
            db,
            request.app.state.container,
            access.project,
            actor_of(access),
            dataset,
            name=values["name"],
            connection_id=values["connection_id"],
            model=values["model"],
            **kwargs,
        )
    except NotAllowed as exc:
        raise HTTPException(403, str(exc)) from exc
    except DatasetError as exc:
        db.rollback()
        return _reopen(request, db, access, dataset, ExperimentForm(values, str(exc)), 400)
    return RedirectResponse(f"{base_url(access.project.id)}/{dataset.id}/runs/{run.id}", 303)


# --- run detail ---------------------------------------------------------------------------------


def _load_run(db: Session, access: Access, dataset_id: str, run_id: str):
    dataset = load_dataset(db, access, dataset_id)
    try:
        run = dataset_runs.get_run(db, access.project, run_id)
    except DatasetNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    if run.dataset_id != dataset.id:
        raise HTTPException(404, "Not found.")
    return dataset, run


def _shown_status(row: DatasetRunItem) -> str:
    """`completed` rows read as Succeeded; anything unknown counts as pending."""
    status = "succeeded" if row.status == "completed" else row.status
    return status if status in STATUS_ORDER else "pending"


def _fmt_seconds(ms: float | None) -> str:
    if ms is None:
        return "-"
    return f"{ms / 1000:.1f} s"


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def run_context(db: Session, access: Access, dataset: Dataset, run: DatasetRun) -> dict[str, Any]:
    project = access.project
    rows = dataset_runs.run_items(db, project, run)
    shown = rows[:ITEM_LIMIT]
    versions = {v.id: v for v in service.items_as_of(db, project, dataset, include_archived=True)}
    by_status = {s: 0 for s in STATUS_ORDER}
    for row in rows:
        by_status[_shown_status(row)] += 1
    total = len(rows)
    finished = by_status["succeeded"] + by_status["failed"] + by_status["capped"]

    executions = list(
        db.scalars(
            select(EvalExecution).where(
                EvalExecution.project_id == project.id,
                EvalExecution.run_item_id.in_([r.id for r in shown] or [""]),
            )
        )
    )
    score_ids = [e.score_id for e in executions if e.score_id]
    scores = {
        s.id: s
        for s in db.scalars(
            select(Score).where(Score.project_id == project.id, Score.id.in_(score_ids or [""]))
        )
    }
    item_scores: dict[str, list[dict[str, str]]] = {}
    numeric: dict[str, list[float]] = {}
    for execution in executions:
        score = scores.get(execution.score_id or "")
        if score is None:
            continue
        shown_value = score.string_value if score.value is None else f"{score.value:g}"
        item_scores.setdefault(execution.run_item_id or "", []).append(
            {"name": score.name, "value": shown_value or ""}
        )
        if score.value is not None:
            numeric.setdefault(score.name, []).append(score.value)

    latencies = [r.latency_ms for r in rows if r.latency_ms is not None]
    cost = sum(r.cost for r in rows if r.cost)
    items = []
    for row in shown:
        version = versions.get(row.item_id)
        items.append(
            {
                "row": row,
                "status": _shown_status(row),
                "input": _text(version.input if version else ""),
                "output": _text(row.output),
                "scores": item_scores.get(row.id, []),
                "latency": _fmt_seconds(row.latency_ms),
                "trace_url": f"/project/{project.id}/traces/{row.trace_id}" if row.trace_id else "",
            }
        )
    done = run.status != "running"
    config = run.config or {}
    starter = db.get(User, run.created_by) if run.created_by else None
    return {
        "dataset": dataset,
        "run": run,
        "base": f"{base_url(project.id)}/{dataset.id}",
        "total": total,
        "finished": finished,
        "by_status": by_status,
        "percent": {s: (by_status[s] / total * 100 if total else 0) for s in STATUS_ORDER},
        "items": items,
        "more": max(0, total - ITEM_LIMIT),
        "done": done,
        "poll_seconds": POLL_SECONDS,
        "mean_latency": _fmt_seconds(sum(latencies) / len(latencies)) if latencies else "-",
        "p95_latency": _fmt_seconds(_percentile(latencies, 0.95)),
        "cost": f"${cost:.3f}",
        "score_means": [
            {"name": name, "mean": f"{sum(v) / len(v):.2f}", "n": len(v)}
            for name, v in sorted(numeric.items())
        ],
        "success_rate": f"{by_status['succeeded'] / finished * 100:.0f}%" if finished else "-",
        "config": config,
        "started_by": (starter.name or starter.email) if starter else "",
        "can_compare": True,
    }


@router.get(
    "/project/{project_id}/datasets/{dataset_id}/runs/{run_id}", response_class=HTMLResponse
)
def run_detail(
    request: Request,
    dataset_id: str,
    run_id: str,
    access: Access = Depends(require(Perm.EVAL_READ)),
    db: Session = Depends(get_db),
):
    dataset, run = _load_run(db, access, dataset_id, run_id)
    context = page_context(db, access, active="datasets", **run_context(db, access, dataset, run))
    return render(request, "datasets/run_detail.html", context)


@router.get(
    "/project/{project_id}/datasets/{dataset_id}/runs/{run_id}/progress",
    response_class=HTMLResponse,
)
def run_progress(
    request: Request,
    dataset_id: str,
    run_id: str,
    access: Access = Depends(require(Perm.EVAL_READ)),
    db: Session = Depends(get_db),
):
    """The polled fragment. Once the run is done it carries no polling trigger, so polling stops."""
    dataset, run = _load_run(db, access, dataset_id, run_id)
    context = page_context(db, access, active="datasets", **run_context(db, access, dataset, run))
    return render(request, "datasets/_run_progress.html", context)
