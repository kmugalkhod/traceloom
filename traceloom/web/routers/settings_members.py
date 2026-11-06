"""Settings > Members & invites, and the accept-invite pages."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from traceloom.auth.deps import (
    Access,
    current_org,
    current_user,
    current_user_optional,
    get_container,
    require,
)
from traceloom.auth.permissions import Perm, Role
from traceloom.container import Container
from traceloom.db.models import Organization, Project, User
from traceloom.db.session import get_db
from traceloom.services import invites, orgs
from traceloom.web.context import build_shell, landing, page_context, settings_tabs
from traceloom.web.routers.auth import safe_next
from traceloom.web.templating import render

router = APIRouter(tags=["members"])

NOTICES = {
    "role": "Role updated.",
    "removed": "Member removed.",
    "invited": "Invite sent.",
    "revoked": "Invite revoked.",
}


def _roles_for(access: Access) -> list[Role]:
    """Roles the acting user may hand out: everything up to Admin, plus Owner for Owners."""
    return [r for r in Role if r is not Role.OWNER or access.role is Role.OWNER]


def _project_of(db: Session, access: Access) -> Project:
    """The project whose Settings we show: the current one, or the org's first (org routes)."""
    project = access.project or db.scalar(
        select(Project).where(Project.org_id == access.org.id).order_by(Project.name, Project.id)
    )
    if project is None:
        raise HTTPException(404, "This organization has no projects.")
    return project


def members_page(
    request: Request, db: Session, access: Access, *, error=None, status=200, notice=""
):
    project = _project_of(db, access)
    access.project = project
    context = page_context(
        db,
        access,
        settings_tabs=settings_tabs(project.id, "members", access.role),
        members=orgs.member_rows(db, access.org.id, access.user, access.role),
        invites=invites.list_invites(db, access.org.id) if access.role >= Role.ADMIN else [],
        assignable_roles=_roles_for(access),
        error=error,
        notice=NOTICES.get(notice, ""),
        back=f"/project/{project.id}/settings/members",
    )
    return render(request, "settings/members.html", context, status=status)


@router.get("/project/{project_id}/settings/members", response_class=HTMLResponse)
def members(
    request: Request,
    notice: str = "",
    access: Access = Depends(require(Perm.TRACES_READ)),
    db: Session = Depends(get_db),
):
    return members_page(request, db, access, notice=notice)


def _done(db: Session, back: str, notice: str, access: Access) -> RedirectResponse:
    target = safe_next(back) if back else f"/project/{_project_of(db, access).id}/settings/members"
    return RedirectResponse(f"{target}?notice={notice}", status_code=303)


def _fail(request: Request, db: Session, access: Access, exc: orgs.OrgError):
    db.rollback()
    if isinstance(exc, orgs.NotAllowed):
        raise HTTPException(403, str(exc)) from exc
    return members_page(request, db, access, error=str(exc), status=400)


@router.post("/orgs/{org_id}/members/{user_id}/role", response_class=HTMLResponse)
def change_role(
    request: Request,
    user_id: str,
    role: str = Form(""),
    back: str = Form(""),
    access: Access = Depends(require(Perm.ROLES)),
    db: Session = Depends(get_db),
):
    try:
        new_role = Role.parse(role)
        orgs.change_role(db, access.user, access.role, access.org.id, user_id, new_role)
    except KeyError:
        return _fail(request, db, access, orgs.OrgError("Choose a valid role."))
    except orgs.OrgError as exc:
        return _fail(request, db, access, exc)
    db.commit()
    return _done(db, back, "role", access)


@router.post("/orgs/{org_id}/members/{user_id}/remove", response_class=HTMLResponse)
def remove_member(
    request: Request,
    user_id: str,
    back: str = Form(""),
    access: Access = Depends(current_org),
    db: Session = Depends(get_db),
):
    """Admins remove others; anyone may remove themselves (leave)."""
    try:
        orgs.remove_member(db, access.user, access.role, access.org.id, user_id)
    except orgs.OrgError as exc:
        return _fail(request, db, access, exc)
    db.commit()
    if user_id == access.user.id:
        return RedirectResponse("/", status_code=303)
    return _done(db, back, "removed", access)


@router.post("/orgs/{org_id}/invites", response_class=HTMLResponse)
def invite(
    request: Request,
    email: str = Form(""),
    role: str = Form("member"),
    back: str = Form(""),
    access: Access = Depends(require(Perm.INVITES)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    try:
        invites.create_invite(
            db,
            container.settings,
            container.mailer,
            access.user,
            access.role,
            access.org,
            email,
            Role.parse(role),
        )
    except KeyError:
        return _fail(request, db, access, orgs.OrgError("Choose a valid role."))
    except orgs.OrgError as exc:
        return _fail(request, db, access, exc)
    db.commit()
    return _done(db, back, "invited", access)


@router.post("/orgs/{org_id}/invites/{invite_id}/revoke", response_class=HTMLResponse)
def revoke_invite(
    request: Request,
    invite_id: str,
    back: str = Form(""),
    access: Access = Depends(require(Perm.INVITES)),
    db: Session = Depends(get_db),
):
    try:
        invites.revoke_invite(db, access.user, access.role, access.org.id, invite_id)
    except orgs.OrgError as exc:
        return _fail(request, db, access, exc)
    db.commit()
    return _done(db, back, "revoked", access)


# --- accept an invite -----------------------------------------------------------------------


def _invite_page(request, template_ctx: dict, status=200):
    return render(request, "orgs/accept_invite.html", template_ctx, status=status)


@router.get("/invite/{token}", response_class=HTMLResponse)
def invite_page(
    request: Request,
    token: str,
    user: User | None = Depends(current_user_optional),
    db: Session = Depends(get_db),
):
    invite_row = invites.find_valid(db, token)
    if invite_row is None:
        return _invite_page(request, {"state": "invalid"}, status=410)
    org = db.get(Organization, invite_row.org_id)
    if user is None:
        if db.scalar(select(User.id).where(User.email == invite_row.email)):
            return RedirectResponse(f"/login?next=/invite/{token}", status_code=303)
        return RedirectResponse(f"/signup?invite={token}", status_code=303)
    context = {
        "state": "ready",
        "org": org,
        "invite": invite_row,
        "role_label": invite_row.role.title(),
        "token": token,
        "user": user,
    }
    if user.email != invite_row.email:
        context["state"] = "wrong_account"
    return _invite_page(request, context, status=200 if context["state"] == "ready" else 403)


@router.post("/invite/{token}/accept", response_class=HTMLResponse)
def accept_invite(
    request: Request,
    token: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    invite_row = invites.find_valid(db, token)
    if invite_row is None:
        return _invite_page(request, {"state": "invalid"}, status=410)
    if user.email != invite_row.email:
        raise HTTPException(403, "This invite was sent to a different email address.")
    invites.accept(db, invite_row, user)
    db.commit()
    shell = build_shell(db, user, org_id=invite_row.org_id)
    return RedirectResponse(landing(shell.project.id) if shell.project else "/", status_code=303)
