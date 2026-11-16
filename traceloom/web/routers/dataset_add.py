"""Add to dataset: from a trace or an observation (prefilled, editable) and in bulk from a
list selection. The dialog is an HTMX fragment on the detail page and a plain page without JS."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.auth.deps import Access, form_data, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.datasets import service
from traceloom.datasets.service import DatasetError, DatasetNotFound, NotAllowed
from traceloom.db.session import get_db
from traceloom.web.context import page_context
from traceloom.web.routers.datasets import actor_of, base_url, parse_json_field, pretty
from traceloom.web.templating import render

router = APIRouter(tags=["dataset-add"])

MAX_BULK = 200  # one request; larger selections go through an import


def _prefill(container: Container, access: Access, trace_ids: list[str], observation_id: str):
    store = container.trace_store
    if len(trace_ids) == 1:
        fill = service.prefill_from_trace(
            store, access.project, trace_ids[0], observation_id or None
        )
        return {
            "input": pretty(fill.input),
            "expected_output": "" if fill.expected_output is None else pretty(fill.expected_output),
            "metadata": pretty(fill.metadata),
            "source": fill.source_observation_id or fill.source_trace_id,
        }
    return {}


def _render(
    request: Request,
    db: Session,
    container: Container,
    access: Access,
    trace_ids: list[str],
    observation_id: str,
    *,
    values: dict[str, str] | None = None,
    error: str = "",
    dataset_id: str = "",
    new_dataset: str = "",
    status: int = 200,
):
    if not trace_ids:
        raise HTTPException(400, "Choose at least one trace.")
    if len(trace_ids) > MAX_BULK:
        raise HTTPException(400, f"Add at most {MAX_BULK} traces at a time.")
    try:
        prefill = _prefill(container, access, trace_ids, observation_id)
    except DatasetNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    ctx: dict[str, Any] = {
        "datasets": [s.dataset for s in service.list_datasets(db, access.project)],
        "trace_ids": trace_ids,
        "observation_id": observation_id,
        "bulk": len(trace_ids) > 1,
        "vals": values or prefill,
        "prefill": prefill,
        "error": error,
        "dataset_id": dataset_id,
        "new_dataset": new_dataset,
        "action": f"{base_url(access.project.id)}/add",
        "trace_url": f"/project/{access.project.id}/traces/",
    }
    hx = request.headers.get("HX-Request") == "true"
    context = page_context(db, access, active="datasets", dialog=hx, **ctx)
    name = "datasets/_add_dialog.html" if hx else "datasets/add.html"
    return render(request, name, context, status=status)


@router.get("/project/{project_id}/datasets/add", response_class=HTMLResponse)
def add_form(
    request: Request,
    trace_id: list[str] = Query(default=[]),
    observation_id: str = "",
    access: Access = Depends(require(Perm.DATASETS_WRITE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    return _render(request, db, container, access, list(trace_id), observation_id)


@router.post("/project/{project_id}/datasets/add", response_class=HTMLResponse)
def add_submit(
    request: Request,
    access: Access = Depends(require(Perm.DATASETS_WRITE)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    trace_ids = [str(t) for t in form.getlist("trace_id") if str(t)]
    observation_id = str(form.get("observation_id", ""))
    dataset_id = str(form.get("dataset_id", ""))
    new_name = str(form.get("new_dataset", "")).strip()
    values = {k: str(form.get(k, "")) for k in ("input", "expected_output", "metadata")}
    actor = actor_of(access)

    def again(message: str, status: int = 400):
        db.rollback()
        return _render(
            request, db, container, access, trace_ids, observation_id,
            values=values or None, error=message, dataset_id=dataset_id, new_dataset=new_name,
            status=status,
        )  # fmt: skip

    try:
        if new_name:
            dataset = service.create_dataset(db, access.project, actor, name=new_name)
        elif dataset_id:
            dataset = service.get_dataset(db, access.project, dataset_id)
        else:
            return again("Choose a dataset, or name a new one.")
        if len(trace_ids) > 1:
            added = skipped = 0
            for tid in trace_ids:
                try:
                    service.add_from_trace(
                        db, container.trace_store, access.project, actor, dataset, trace_id=tid
                    )
                    added += 1
                except DatasetError:  # a trace with no input, or deleted since the list loaded
                    skipped += 1
                except DatasetNotFound:
                    skipped += 1
            if not added:
                return again("None of the selected traces could be added (no input or not found).")
            target = f"?notice=bulk&added={added}&skipped={skipped}"
        else:
            fields: dict[str, Any] = {
                "input": parse_json_field(values["input"], "Input", required=True),
                "expected_output": parse_json_field(values["expected_output"], "Expected output"),
                "metadata": parse_json_field(values["metadata"], "Metadata") or {},
            }
            if not isinstance(fields["metadata"], dict):
                raise DatasetError("Metadata must be a JSON object.")
            service.add_from_trace(
                db, container.trace_store, access.project, actor, dataset,
                trace_id=trace_ids[0], observation_id=observation_id or None, **fields,
            )  # fmt: skip
            target = "?notice=traceadded"
    except service.DatasetConflict as exc:
        return again(str(exc), 409)
    except DatasetNotFound as exc:
        db.rollback()
        raise HTTPException(404, "Not found.") from exc
    except DatasetError as exc:
        return again(str(exc))
    except NotAllowed as exc:
        raise HTTPException(403, str(exc)) from exc
    db.commit()
    return RedirectResponse(f"{base_url(access.project.id)}/{dataset.id}{target}", 303)
