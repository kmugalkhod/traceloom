"""Datasets: the list, the detail page (Items | Runs | History), and create / delete."""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.auth.deps import Access, form_data, require
from traceloom.auth.permissions import Perm
from traceloom.datasets import runs as dataset_runs
from traceloom.datasets import service
from traceloom.datasets.service import Actor, DatasetError, DatasetNotFound, ItemView, NotAllowed
from traceloom.db.models import User
from traceloom.db.models_datasets import Dataset, DatasetRunItem
from traceloom.db.session import get_db
from traceloom.web.context import page_context
from traceloom.web.templating import render

router = APIRouter(tags=["datasets"])

PAGE_SIZE = 50
TABS = ("items", "runs", "history")
NOTES = {
    "created": "Dataset created.",
    "saved": "Item saved as a new version.",
    "added": "Item added.",
    "archived": "Item archived. Runs started earlier still read it.",
    "traceadded": "Added to this dataset with a link to its source.",
}


def actor_of(access: Access) -> Actor:
    return Actor.for_user(access.user, access.role)


def base_url(project_id: str) -> str:
    return f"/project/{project_id}/datasets"


def load_dataset(db: Session, access: Access, dataset_id: str) -> Dataset:
    try:
        return service.get_dataset(db, access.project, dataset_id)
    except DatasetNotFound as exc:
        raise HTTPException(404, "Not found.") from exc


def pretty(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def compact(value: Any) -> str:
    return "" if value is None else json.dumps(value, ensure_ascii=False, default=str)


def parse_json_field(text: str, label: str, *, required: bool = False) -> Any:
    """A JSON editor's text as a value. An empty optional field is None."""
    text = (text or "").strip()
    if not text:
        if required:
            raise DatasetError(f"{label} is required.")
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{label}: line {exc.lineno}: {exc.msg}.") from exc


@dataclass
class FormError:
    """A dialog or drawer to reopen on the page with the user's text and a message."""

    scope: str  # "new" | item id
    message: str
    values: dict[str, str]


def _names(db: Session, ids: set[str | None]) -> dict[str, str]:
    users = db.scalars(select(User).where(User.id.in_({i for i in ids if i})))
    return {u.id: (u.name or u.email) for u in users}


def _diff(old: ItemView | None, new: ItemView) -> list[tuple[str, str]]:
    """Changed lines between two versions of an item as ("add" | "del", text)."""
    if old is None:
        return []
    a = {"input": old.input, "expected_output": old.expected_output, "metadata": old.metadata}
    b = {"input": new.input, "expected_output": new.expected_output, "metadata": new.metadata}
    lines = difflib.unified_diff(pretty(a).splitlines(), pretty(b).splitlines(), lineterm="", n=0)
    out = []
    for line in lines:
        if line.startswith(("---", "+++", "@@")):
            continue
        out.append(("add" if line.startswith("+") else "del", line))
    return out


def _change(version: ItemView) -> tuple[str, str]:
    if version.status == "archived":
        return "Archived", "skip"
    if version.version == 1:
        return "Created", ""
    return "Edited", "lab"


def _bulk_notice(bulk: tuple[int, int] | None) -> str:
    if not bulk:
        return ""
    added, skipped = bulk
    text = f"Added {added} trace{'' if added == 1 else 's'} as items, each with a source link."
    return text + (f" {skipped} could not be added (no input or not found)." if skipped else "")


def render_detail(
    request: Request,
    db: Session,
    access: Access,
    dataset: Dataset,
    *,
    tab: str = "items",
    status: str = "active",
    q: str = "",
    page: int = 1,
    open_item: str = "",
    notice: str = "",
    error: FormError | None = None,
    delete_error: str = "",
    bulk: tuple[int, int] | None = None,
    http_status: int = 200,
):
    project, actor = access.project, actor_of(access)
    tab = tab if tab in TABS else "items"
    base = f"{base_url(project.id)}/{dataset.id}"
    everything = service.items_as_of(db, project, dataset, include_archived=True)
    active = [v for v in everything if v.status == "active"]
    archived = [v for v in everything if v.status == "archived"]
    ctx: dict[str, Any] = {
        "dataset": dataset,
        "version": service.dataset_version(db, dataset),
        "tab": tab,
        "base": base,
        "counts": {"items": len(active), "archived": len(archived)},
        "can_write": actor.can(Perm.DATASETS_WRITE),
        "can_delete": actor.can(Perm.DATASETS_DELETE),
        "notice": NOTES.get(notice, "") or _bulk_notice(bulk),
        "error": error,
        "delete_error": delete_error,
        "compact": compact,
        "pretty": pretty,
        "q": q,
        "status": status if status in ("active", "archived") else "active",
    }
    run_list = dataset_runs.list_runs(db, project, dataset)
    ctx["counts"]["runs"] = len(run_list)

    if tab == "items":
        shown = archived if ctx["status"] == "archived" else active
        needle = q.strip().lower()
        if needle:
            shown = [
                v
                for v in shown
                if needle in (compact(v.input) + compact(v.expected_output)).lower()
            ]
        total = len(shown)
        ctx["items"] = shown[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        ctx["total"] = total
        params = {"status": ctx["status"] if ctx["status"] != "active" else "", "q": q}

        def page_url(n: int) -> str:
            query = urlencode(
                {**{k: v for k, v in params.items() if v}, **({"page": n} if n > 1 else {})}
            )
            return f"{base}" + (f"?{query}" if query else "")

        ctx["prev_url"] = page_url(page - 1) if page > 1 else ""
        ctx["next_url"] = page_url(page + 1) if page * PAGE_SIZE < total else ""
    elif tab == "runs":
        counts = dict(
            db.execute(
                select(DatasetRunItem.run_id, func.count())
                .where(DatasetRunItem.project_id == project.id)
                .group_by(DatasetRunItem.run_id)
            ).all()
        )
        ctx["runs"] = [(r, counts.get(r.id, 0)) for r in run_list]
    else:
        history = service.dataset_history(db, project, dataset)
        names = _names(db, {h.actor for h in history})
        by_key = {(h.id, h.version): h for h in history}
        ctx["history"] = [
            {
                "v": h,
                "who": names.get(h.actor, "API key" if h.actor else ""),
                "change": _change(h),
                "diff": _diff(by_key.get((h.id, h.version - 1)), h) if h.status == "active" else [],
            }
            for h in history
        ]

    if open_item:
        try:
            item = service.get_item(db, project, open_item)
            if item.dataset_id != dataset.id:
                raise DatasetNotFound(open_item)
            versions = service.item_history(db, project, item)
        except DatasetNotFound:
            versions = []
        if versions:
            names = _names(db, {v.actor for v in versions})
            ctx["drawer"] = {
                "item": versions[0],
                "versions": [
                    (v, names.get(v.actor, "API key" if v.actor else "")) for v in versions
                ],
            }
    ctx["trace_url"] = lambda tid: f"/project/{project.id}/traces/{tid}"
    context = page_context(db, access, active="datasets", **ctx)
    return render(request, "datasets/detail.html", context, status=http_status)


# --- list and create --------------------------------------------------------------------------


def render_list(
    request: Request,
    db: Session,
    access: Access,
    *,
    error: str = "",
    values: dict[str, str] | None = None,
    http_status: int = 200,
):
    summaries = service.list_datasets(db, access.project)
    actor = actor_of(access)
    context = page_context(
        db,
        access,
        active="datasets",
        summaries=summaries,
        base=base_url(access.project.id),
        can_write=actor.can(Perm.DATASETS_WRITE),
        can_delete=actor.can(Perm.DATASETS_DELETE),
        error=error,
        values=values or {"name": "", "description": "", "metadata": ""},
    )
    return render(request, "datasets/list.html", context, status=http_status)


@router.get("/project/{project_id}/datasets", response_class=HTMLResponse)
def dataset_list(
    request: Request,
    access: Access = Depends(require(Perm.EVAL_READ)),
    db: Session = Depends(get_db),
):
    return render_list(request, db, access)


@router.post("/project/{project_id}/datasets", response_class=HTMLResponse)
def create_dataset(
    request: Request,
    access: Access = Depends(require(Perm.DATASETS_WRITE)),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    values = {k: str(form.get(k, "")) for k in ("name", "description", "metadata")}
    try:
        metadata = parse_json_field(values["metadata"], "Metadata")
        dataset = service.create_dataset(
            db,
            access.project,
            actor_of(access),
            name=values["name"],
            description=values["description"],
            metadata=metadata,
        )
    except service.DatasetConflict:
        db.rollback()
        message = f"A dataset named {values['name'].strip()} already exists in this project."
        return render_list(request, db, access, error=message, values=values, http_status=409)
    except DatasetError as exc:
        db.rollback()
        return render_list(request, db, access, error=str(exc), values=values, http_status=400)
    except NotAllowed as exc:
        raise HTTPException(403, str(exc)) from exc
    db.commit()
    return RedirectResponse(f"{base_url(access.project.id)}/{dataset.id}?notice=created", 303)


# --- detail and delete ------------------------------------------------------------------------


@router.get("/project/{project_id}/datasets/{dataset_id}", response_class=HTMLResponse)
def dataset_detail(
    request: Request,
    dataset_id: str,
    tab: str = "items",
    status: str = "active",
    q: str = "",
    page: int = 1,
    item: str = "",
    notice: str = "",
    added: int = 0,
    skipped: int = 0,
    access: Access = Depends(require(Perm.EVAL_READ)),
    db: Session = Depends(get_db),
):
    dataset = load_dataset(db, access, dataset_id)
    return render_detail(
        request,
        db,
        access,
        dataset,
        tab=tab,
        status=status,
        q=q,
        page=max(page, 1),
        open_item=item,
        notice=notice,
        bulk=(added, skipped) if notice == "bulk" else None,
    )


@router.post("/project/{project_id}/datasets/{dataset_id}/delete", response_class=HTMLResponse)
def delete_dataset(
    request: Request,
    dataset_id: str,
    access: Access = Depends(require(Perm.DATASETS_DELETE)),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    dataset = load_dataset(db, access, dataset_id)
    if str(form.get("confirm_name", "")).strip() != dataset.name:
        return render_detail(
            request,
            db,
            access,
            dataset,
            delete_error="Type the dataset name exactly to delete it.",
            http_status=400,
        )
    service.delete_dataset(db, access.project, actor_of(access), dataset)
    db.commit()
    return RedirectResponse(base_url(access.project.id), 303)
