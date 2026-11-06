"""Home redirect, create organization, project settings and delete organization."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from traceloom.auth.deps import Access, current_user, current_user_optional, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.models import User
from traceloom.db.session import get_db
from traceloom.services import orgs
from traceloom.web.context import build_shell, landing, page_context, settings_tabs
from traceloom.web.routers.settings_members import members_page
from traceloom.web.templating import render

router = APIRouter(tags=["orgs"])


@router.get("/")
def home(user: User | None = Depends(current_user_optional), db: Session = Depends(get_db)):
    if user is None:
        return RedirectResponse("/login", status_code=303)
    shell = build_shell(db, user)
    if shell.project is not None:
        return RedirectResponse(landing(shell.project.id), status_code=303)
    return RedirectResponse("/orgs/new", status_code=303)


def _new_org_page(
    request: Request, db: Session, user: User, *, values=None, error=None, status=200
):
    context = build_shell(db, user).context()
    context.update(values=values or {"name": "", "project": "my-project"}, error=error)
    return render(request, "orgs/new.html", context, status=status)


@router.get("/orgs/new", response_class=HTMLResponse)
def new_org_page(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    return _new_org_page(request, db, user)


@router.post("/orgs/new", response_class=HTMLResponse)
def new_org(
    request: Request,
    name: str = Form(""),
    project: str = Form("my-project"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        _, first_project = orgs.create_org(db, user, name, project)
    except orgs.OrgError as exc:
        db.rollback()
        return _new_org_page(
            request, db, user, values={"name": name, "project": project}, error=str(exc), status=400
        )
    db.commit()
    return RedirectResponse(f"/project/{first_project.id}/settings/keys", status_code=303)


@router.post("/orgs/{org_id}/projects", response_class=HTMLResponse)
def new_project(
    request: Request,
    name: str = Form(""),
    access: Access = Depends(require(Perm.PROJECT_CREATE)),
    db: Session = Depends(get_db),
):
    try:
        project = orgs.create_project(db, access.user, access.role, access.org.id, name)
    except orgs.OrgError as exc:
        db.rollback()
        return _general_page(request, db, access, error=str(exc), status=400, new_project_name=name)
    db.commit()
    return RedirectResponse(f"/project/{project.id}/settings", status_code=303)


def _general_page(request, db, access, *, error=None, status=200, new_project_name="", saved=False):
    context = page_context(
        db,
        access,
        settings_tabs=settings_tabs(access.project.id, "general", access.role),
        error=error,
        saved=saved,
        new_project_name=new_project_name,
        values={"name": access.project.name},
    )
    return render(request, "settings/general.html", context, status=status)


@router.get("/project/{project_id}/settings", response_class=HTMLResponse)
def general_settings(
    request: Request,
    saved: int = 0,
    access: Access = Depends(require(Perm.TRACES_READ)),
    db: Session = Depends(get_db),
):
    return _general_page(request, db, access, saved=bool(saved))


@router.post("/project/{project_id}/settings", response_class=HTMLResponse)
def save_general_settings(
    request: Request,
    name: str = Form(""),
    access: Access = Depends(require(Perm.PROJECT_SETTINGS)),
    db: Session = Depends(get_db),
):
    try:
        orgs.rename_project(db, access.user, access.role, access.project, name)
    except orgs.OrgError as exc:
        db.rollback()
        context = page_context(
            db,
            access,
            settings_tabs=settings_tabs(access.project.id, "general", access.role),
            error=str(exc),
            saved=False,
            new_project_name="",
            values={"name": name},
        )
        return render(request, "settings/general.html", context, status=400)
    db.commit()
    return RedirectResponse(f"/project/{access.project.id}/settings?saved=1", status_code=303)


@router.post("/orgs/{org_id}/delete", response_class=HTMLResponse)
def delete_org(
    request: Request,
    confirm_name: str = Form(""),
    back: str = Form(""),
    access: Access = Depends(require(Perm.ORG_DELETE)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    if confirm_name != access.org.name:
        return members_page(
            request, db, access, error="Type the organization name exactly to confirm.", status=400
        )
    orgs.delete_org(db, container, access.user, access.role, access.org)
    db.commit()
    return RedirectResponse("/", status_code=303)
