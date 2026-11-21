"""Import wizard: upload -> map columns -> preview -> import (FR-D3).

The upload is parked in the BlobStore under a random token between steps, so the later steps
post only the column mapping. It is deleted when the import finishes.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile

from traceloom.auth.deps import Access, form_data, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.datasets import importer, service
from traceloom.datasets.importer import ImportLimitError, Mapping, Table
from traceloom.datasets.service import DatasetError
from traceloom.db.models_datasets import Dataset
from traceloom.db.session import get_db
from traceloom.ports import BlobNotFound
from traceloom.web.context import page_context
from traceloom.web.routers.datasets import actor_of, base_url, compact, load_dataset
from traceloom.web.templating import render

router = APIRouter(tags=["dataset-import"])

TOKEN_RE = re.compile(r"[0-9a-f]{32}")
USES = ("ignore", "input", "expected_output", "metadata")
STEPS = ("Upload", "Map columns", "Preview", "Done")


def _key(project_id: str, token: str, fmt: str) -> str:
    return f"dataset-imports/{project_id}/{token}.{fmt}"


def _load(container: Container, project_id: str, token: str) -> tuple[Table, str]:
    """The parked upload as a Table, or 404 when the token is unknown or expired."""
    if not TOKEN_RE.fullmatch(token):
        raise HTTPException(404, "Not found.")
    settings = container.settings
    for fmt in importer.FORMATS:
        try:
            data = container.blob_store.get(_key(project_id, token, fmt))
        except BlobNotFound:
            continue
        table = importer.read_table(data, fmt, max_rows=settings.dataset_import_max_rows)
        return table, fmt
    raise HTTPException(404, "This upload has expired. Upload the file again.")


def _render(
    request: Request,
    db: Session,
    access: Access,
    dataset: Dataset,
    step: int,
    *,
    status: int = 200,
    **extra: Any,
):
    base = f"{base_url(access.project.id)}/{dataset.id}"
    settings = request.app.state.settings
    context = page_context(
        db,
        access,
        active="datasets",
        dataset=dataset,
        base=base,
        step=step,
        steps=STEPS,
        max_rows=settings.dataset_import_max_rows,
        compact=compact,
        **extra,
    )
    return render(request, "datasets/import.html", context, status=status)


def _mapping_from(form: FormData, table: Table) -> Mapping:
    uses = {c: str(form.get(f"use_{i}", "ignore")) for i, c in enumerate(table.columns)}
    if any(u not in USES for u in uses.values()):
        raise DatasetError("Choose a valid use for every column.")
    inputs = [c for c, u in uses.items() if u == "input"]
    expected = [c for c, u in uses.items() if u == "expected_output"]
    if len(inputs) != 1:
        raise DatasetError("Choose which one column is the input to continue.")
    if len(expected) > 1:
        raise DatasetError("Only one column can be the expected output.")
    return Mapping(
        inputs[0],
        expected[0] if expected else None,
        [c for c, u in uses.items() if u == "metadata"],
    )


def _uses(table: Table, mapping: Mapping | None) -> dict[int, str]:
    mapping = mapping or importer.default_mapping(table.columns)
    out = {}
    for i, column in enumerate(table.columns):
        if mapping and column == mapping.input:
            out[i] = "input"
        elif mapping and column == mapping.expected_output:
            out[i] = "expected_output"
        elif mapping and column in mapping.metadata:
            out[i] = "metadata"
        else:
            out[i] = "ignore"
    return out


def _map_step(request, db, access, dataset, token, table, *, error="", form=None, status=200):
    mapping = None
    if form is not None:
        mapping = Mapping(
            next((c for i, c in enumerate(table.columns) if form.get(f"use_{i}") == "input"), ""),
            next(
                (
                    c
                    for i, c in enumerate(table.columns)
                    if form.get(f"use_{i}") == "expected_output"
                ),
                None,
            ),
            [c for i, c in enumerate(table.columns) if form.get(f"use_{i}") == "metadata"],
        )
    return _render(
        request, db, access, dataset, 2, status=status, token=token, table=table, error=error,
        uses=_uses(table, mapping), sample=table.records[0] if table.records else {},
        use_options=USES,
    )  # fmt: skip


@router.get("/project/{project_id}/datasets/{dataset_id}/import", response_class=HTMLResponse)
def import_start(
    request: Request,
    dataset_id: str,
    access: Access = Depends(require(Perm.DATASETS_WRITE)),
    db: Session = Depends(get_db),
):
    dataset = load_dataset(db, access, dataset_id)
    return _render(request, db, access, dataset, 1, error="")


@router.post("/project/{project_id}/datasets/{dataset_id}/import", response_class=HTMLResponse)
def import_upload(
    request: Request,
    dataset_id: str,
    access: Access = Depends(require(Perm.DATASETS_WRITE)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    upload = form.get("file")
    dataset = load_dataset(db, access, dataset_id)
    settings = container.settings
    try:
        if not isinstance(upload, UploadFile) or not upload.filename:
            raise ImportLimitError("Choose a file to upload.")
        fmt = importer.detect_format(upload.filename)
        data = upload.file.read(settings.ingest_max_bytes + 1)
        importer.read_table(  # validates the file before it is parked
            data,
            fmt,
            max_rows=settings.dataset_import_max_rows,
            max_bytes=settings.ingest_max_bytes,
        )
    except ImportLimitError as exc:
        return _render(request, db, access, dataset, 1, status=400, error=str(exc))
    token = uuid.uuid4().hex
    container.blob_store.put(_key(access.project.id, token, fmt), data)
    return RedirectResponse(f"{base_url(access.project.id)}/{dataset.id}/import/{token}", 303)


@router.get(
    "/project/{project_id}/datasets/{dataset_id}/import/{token}", response_class=HTMLResponse
)
def import_map(
    request: Request,
    dataset_id: str,
    token: str,
    access: Access = Depends(require(Perm.DATASETS_WRITE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    dataset = load_dataset(db, access, dataset_id)
    table, _ = _load(container, access.project.id, token)
    return _map_step(request, db, access, dataset, token, table)


@router.post(
    "/project/{project_id}/datasets/{dataset_id}/import/{token}/preview",
    response_class=HTMLResponse,
)
def import_preview(
    request: Request,
    dataset_id: str,
    token: str,
    access: Access = Depends(require(Perm.DATASETS_WRITE)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    dataset = load_dataset(db, access, dataset_id)
    table, _ = _load(container, access.project.id, token)
    try:
        mapping = _mapping_from(form, table)
        preview = importer.preview(table, mapping)
    except DatasetError as exc:
        return _map_step(
            request, db, access, dataset, token, table, error=str(exc), form=form, status=400
        )
    return _render(
        request, db, access, dataset, 3, token=token, table=table, mapping=mapping, preview=preview,
        uses=_uses(table, mapping), file_rows=len(table.records),
    )  # fmt: skip


@router.post(
    "/project/{project_id}/datasets/{dataset_id}/import/{token}/run", response_class=HTMLResponse
)
def import_run(
    request: Request,
    dataset_id: str,
    token: str,
    access: Access = Depends(require(Perm.DATASETS_WRITE)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    dataset = load_dataset(db, access, dataset_id)
    table, fmt = _load(container, access.project.id, token)
    try:
        mapping = _mapping_from(form, table)
        result = importer.import_rows(db, access.project, actor_of(access), dataset, table, mapping)
    except DatasetError as exc:
        db.rollback()
        return _map_step(
            request, db, access, dataset, token, table, error=str(exc), form=form, status=400
        )
    db.commit()
    container.blob_store.delete(_key(access.project.id, token, fmt))
    return _render(
        request, db, access, dataset, 4, result=result, version=service.dataset_version(db, dataset)
    )
