"""Settings > API keys: create (secret shown once), list, revoke."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceloom.auth.api_keys import generate_key_pair, hash_secret
from traceloom.auth.deps import Access, get_container, require, revoke_api_key
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.models import ApiKey
from traceloom.db.session import get_db
from traceloom.services import audit
from traceloom.web.context import page_context, settings_tabs
from traceloom.web.templating import render

router = APIRouter(tags=["api-keys"])

NOTES = {"revoked": "Key revoked. It is rejected with 401 from now on."}


def _keys_page(
    request, db, access, container, *, created=None, error=None, notice="", status=200, note=""
):
    keys = list(
        db.scalars(
            select(ApiKey)
            .where(ApiKey.project_id == access.project.id)
            .order_by(ApiKey.created_at.desc(), ApiKey.id)
        )
    )
    context = page_context(
        db,
        access,
        settings_tabs=settings_tabs(access.project.id, "keys", access.role),
        keys=keys,
        created=created,
        error=error,
        notice=NOTES.get(notice, ""),
        note=note,
        base_url=container.settings.base_url.rstrip("/"),
    )
    # A page that shows a secret must not be cached or kept in history snapshots.
    headers = {"Cache-Control": "no-store"} if created else None
    return render(request, "settings/keys.html", context, status=status, headers=headers)


@router.get("/project/{project_id}/settings/keys", response_class=HTMLResponse)
def keys_page(
    request: Request,
    notice: str = "",
    access: Access = Depends(require(Perm.API_KEYS)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    return _keys_page(request, db, access, container, notice=notice)


@router.post("/project/{project_id}/settings/keys", response_class=HTMLResponse)
def create_key(
    request: Request,
    note: str = Form(""),
    may_move_protected_labels: bool = Form(False),
    access: Access = Depends(require(Perm.API_KEYS)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    note = " ".join(note.split())
    if len(note) > 200:
        return _keys_page(
            request,
            db,
            access,
            container,
            error="The note must be 200 characters or fewer.",
            status=400,
            note=note,
        )
    public_key, secret_key = generate_key_pair()
    key = ApiKey(
        project_id=access.project.id,
        public_key=public_key,
        secret_hash=hash_secret(secret_key, container.settings.secret_key),
        note=note,
        may_move_protected_labels=may_move_protected_labels,
    )
    db.add(key)
    db.flush()
    audit.record(
        db,
        action="api_key.created",
        actor_type="user",
        actor_id=access.user.id,
        org_id=access.org.id,
        project_id=access.project.id,
        target=key.id,
        detail={
            "public_key": public_key,
            "note": note,
            "may_move_protected_labels": may_move_protected_labels,
        },
    )
    db.commit()
    # The secret exists only in this response. Only its hash is stored.
    return _keys_page(
        request,
        db,
        access,
        container,
        created={"public": public_key, "secret": secret_key},
        status=201,
    )


@router.post("/project/{project_id}/settings/keys/{key_id}/revoke")
def revoke_key(
    key_id: str,
    access: Access = Depends(require(Perm.API_KEYS)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    key = db.get(ApiKey, key_id)
    if key is None or key.project_id != access.project.id:
        raise HTTPException(404, "Not found.")
    if revoke_api_key(db, container, key.id):  # also evicts it from the key cache
        audit.record(
            db,
            action="api_key.revoked",
            actor_type="user",
            actor_id=access.user.id,
            org_id=access.org.id,
            project_id=access.project.id,
            target=key.id,
            detail={"public_key": key.public_key},
        )
    db.commit()
    return RedirectResponse(
        f"/project/{access.project.id}/settings/keys?notice=revoked", status_code=303
    )
