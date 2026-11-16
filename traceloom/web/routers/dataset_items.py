"""Dataset items: add, edit (a new version per save) and archive, from the item drawer."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.auth.deps import Access, form_data, require
from traceloom.auth.permissions import Perm
from traceloom.datasets import service
from traceloom.datasets.service import DatasetError, DatasetNotFound, NotAllowed
from traceloom.db.models_datasets import DatasetItem
from traceloom.db.session import get_db
from traceloom.web.routers.datasets import (
    FormError,
    actor_of,
    base_url,
    load_dataset,
    parse_json_field,
    render_detail,
)

router = APIRouter(tags=["dataset-items"])

FIELDS = ("input", "expected_output", "metadata")


def _values(form: FormData) -> dict[str, str]:
    return {k: str(form.get(k, "")) for k in FIELDS}


def _parse(values: dict[str, str]) -> dict[str, object]:
    metadata = parse_json_field(values["metadata"], "Metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise DatasetError("Metadata must be a JSON object.")
    return {
        "input": parse_json_field(values["input"], "Input", required=True),
        "expected_output": parse_json_field(values["expected_output"], "Expected output"),
        "metadata": metadata if metadata is not None else {},
    }


def _item(db: Session, access: Access, dataset_id: str, item_id: str) -> DatasetItem:
    dataset = load_dataset(db, access, dataset_id)
    try:
        item = service.get_item(db, access.project, item_id)
    except DatasetNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    if item.dataset_id != dataset.id:
        raise HTTPException(404, "Not found.")
    return item


@router.get("/project/{project_id}/datasets/{dataset_id}/items/{item_id}")
def item_page(
    dataset_id: str,
    item_id: str,
    access: Access = Depends(require(Perm.EVAL_READ)),
    db: Session = Depends(get_db),
):
    item = _item(db, access, dataset_id, item_id)
    return RedirectResponse(f"{base_url(access.project.id)}/{dataset_id}?item={item.id}", 303)


@router.post("/project/{project_id}/datasets/{dataset_id}/items", response_class=HTMLResponse)
def add_item(
    request: Request,
    dataset_id: str,
    access: Access = Depends(require(Perm.DATASETS_WRITE)),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    dataset = load_dataset(db, access, dataset_id)
    values = _values(form)
    try:
        service.create_item(db, access.project, actor_of(access), dataset, **_parse(values))
    except DatasetError as exc:
        db.rollback()
        error = FormError("new", str(exc), values)
        return render_detail(request, db, access, dataset, error=error, http_status=400)
    except NotAllowed as exc:
        raise HTTPException(403, str(exc)) from exc
    db.commit()
    return RedirectResponse(f"{base_url(access.project.id)}/{dataset.id}?notice=added", 303)


@router.post(
    "/project/{project_id}/datasets/{dataset_id}/items/{item_id}", response_class=HTMLResponse
)
def edit_item(
    request: Request,
    dataset_id: str,
    item_id: str,
    access: Access = Depends(require(Perm.DATASETS_WRITE)),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    item = _item(db, access, dataset_id, item_id)
    dataset = load_dataset(db, access, dataset_id)
    values = _values(form)
    try:
        service.update_item(db, access.project, actor_of(access), item, **_parse(values))
    except DatasetError as exc:
        db.rollback()
        error = FormError(item.id, str(exc), values)
        return render_detail(
            request, db, access, dataset, open_item=item.id, error=error, http_status=400
        )
    except NotAllowed as exc:
        raise HTTPException(403, str(exc)) from exc
    db.commit()
    return RedirectResponse(
        f"{base_url(access.project.id)}/{dataset.id}?item={item.id}&notice=saved", 303
    )


@router.post("/project/{project_id}/datasets/{dataset_id}/items/{item_id}/archive")
def archive_item(
    dataset_id: str,
    item_id: str,
    access: Access = Depends(require(Perm.DATASETS_WRITE)),
    db: Session = Depends(get_db),
):
    item = _item(db, access, dataset_id, item_id)
    service.archive_item(db, access.project, actor_of(access), item)
    db.commit()
    return RedirectResponse(f"{base_url(access.project.id)}/{dataset_id}?notice=archived", 303)
