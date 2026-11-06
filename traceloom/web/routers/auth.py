"""Sign-up, log in, log out and password reset pages."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from traceloom.auth.deps import current_user_optional, enforce_auth_rate_limit, get_container
from traceloom.auth.sessions import clear_session_cookie, issue_session, set_session_cookie
from traceloom.container import Container
from traceloom.db.models import User
from traceloom.db.session import get_db
from traceloom.services import accounts, invites
from traceloom.web.templating import render

router = APIRouter(tags=["auth"])


def safe_next(target: str | None) -> str:
    """Only same-site paths: no scheme, no host, no protocol-relative or backslash tricks."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def _signed_in(response: Response, container: Container, user: User) -> Response:
    set_session_cookie(
        response,
        container.settings,
        issue_session(container.settings, user.id, user.session_version),
    )
    return response


def _rate_limited(
    request: Request, template: str, context: dict, exc: HTTPException
) -> HTMLResponse:
    wait = int(exc.headers.get("Retry-After", "60")) if exc.headers else 60
    count, _, unit = request.app.state.container.settings.rate_auth.partition("/")
    limit = f"{count} attempts a {unit}"
    context = {**context, "rate_limited": {"wait": wait, "limit": limit}}
    return render(request, template, context, status=429, headers={"Retry-After": str(wait)})


# --- sign-up -----------------------------------------------------------------------------------


def _signup_context(container: Container, db: Session, invite_token: str, **extra) -> dict:
    invite = invites.find_valid(db, invite_token) if invite_token else None
    return {
        "invite_only": not container.settings.signup_enabled and invite is None,
        "invite_token": invite_token if invite else "",
        "invite_email": invite.email if invite else "",
        "values": {"name": "", "email": invite.email if invite else ""},
        "errors": {},
        **extra,
    }


@router.get("/signup", response_class=HTMLResponse)
def signup_page(
    request: Request,
    invite: str = "",
    user: User | None = Depends(current_user_optional),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return render(request, "auth/signup.html", _signup_context(container, db, invite))


@router.post("/signup", response_class=HTMLResponse)
def signup(
    request: Request,
    email: str = Form(""),
    name: str = Form(""),
    password: str = Form(""),
    invite: str = Form(""),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    context = _signup_context(container, db, invite)
    context["values"] = {"name": name, "email": context["invite_email"] or email}
    try:
        enforce_auth_rate_limit(request, email)
    except HTTPException as exc:
        return _rate_limited(request, "auth/signup.html", context, exc)

    invite_row = invites.find_valid(db, invite) if invite else None
    if invite_row is not None and email.strip().lower() != invite_row.email:
        email = invite_row.email  # an invite link is only good for the address it was sent to
    try:
        address = accounts.normalize_email(email)
        # Invite-only first step: the email alone is enough to find out whether we can continue.
        if not password and context["invite_only"]:
            if not accounts.has_pending_invite(db, address):
                raise accounts.SignupClosed(
                    "No invite was found for this address. Ask an Owner or Admin to invite you.",
                    "email",
                )
            context.update(
                invite_only=False, values={"name": name, "email": address}, step_two=True
            )
            return render(request, "auth/signup.html", context)
        user = accounts.register(
            db,
            container.settings,
            email=address,
            name=name,
            password=password,
            invited=invite_row is not None,
        )
    except accounts.AccountError as exc:
        context["errors"] = {exc.field or "form": str(exc)}
        return render(request, "auth/signup.html", context, status=400)

    if invite_row is not None:
        invites.accept(db, invite_row, user)
    else:  # signed-up with an open door but an invite for this address is waiting: honour it
        _accept_waiting_invites(db, user)
    db.commit()
    return _signed_in(RedirectResponse("/", status_code=303), container, user)


def _accept_waiting_invites(db: Session, user: User) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import select

    from traceloom.db.models import Invite

    waiting = db.scalars(
        select(Invite).where(
            Invite.email == user.email,
            Invite.accepted_at.is_(None),
            Invite.expires_at > datetime.now(UTC),
        )
    )
    for invite in waiting:
        invites.accept(db, invite, user)


# --- log in / out ------------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next: str = "",
    reset: int = 0,
    user: User | None = Depends(current_user_optional),
):
    if user is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(
        request,
        "auth/login.html",
        {
            "next": safe_next(next) if next else "",
            "reset_done": bool(reset),
            "values": {"email": ""},
            "errors": {},
        },
    )


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    context = {
        "next": safe_next(next) if next else "",
        "values": {"email": email},
        "errors": {},
        "reset_done": False,
    }
    try:
        enforce_auth_rate_limit(request, email)
    except HTTPException as exc:
        return _rate_limited(request, "auth/login.html", context, exc)
    user = accounts.authenticate(db, email, password)
    if user is None:
        context["errors"] = {"form": "The email or password is incorrect."}
        return render(request, "auth/login.html", context, status=401)
    db.commit()  # a transparent password rehash, if any
    return _signed_in(RedirectResponse(safe_next(next), status_code=303), container, user)


@router.post("/logout")
def logout(container: Container = Depends(get_container)):
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response, container.settings)
    return response


# --- password reset ----------------------------------------------------------------------------


@router.get("/forgot", response_class=HTMLResponse)
def forgot_page(request: Request):
    return render(request, "auth/forgot.html", {"values": {"email": ""}, "sent": False})


@router.post("/forgot", response_class=HTMLResponse)
def forgot(
    request: Request,
    email: str = Form(""),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    context = {"values": {"email": email}, "sent": False}
    try:
        enforce_auth_rate_limit(request, email)
    except HTTPException as exc:
        return _rate_limited(request, "auth/forgot.html", context, exc)
    accounts.request_reset(db, container.settings, container.mailer, email)
    # The same page for every address, registered or not.
    return render(
        request,
        "auth/forgot.html",
        {"values": {"email": ""}, "sent": True, "minutes": container.settings.reset_token_minutes},
    )


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_page(request: Request, token: str, db: Session = Depends(get_db)):
    if accounts.reset_status(db, token) != "valid":
        return render(request, "auth/reset.html", {"valid": False, "errors": {}}, status=410)
    return render(request, "auth/reset.html", {"valid": True, "errors": {}})


@router.post("/reset/{token}", response_class=HTMLResponse)
def reset(
    request: Request,
    token: str,
    password: str = Form(""),
    password2: str = Form(""),
    db: Session = Depends(get_db),
):
    if accounts.reset_status(db, token) != "valid":
        return render(request, "auth/reset.html", {"valid": False, "errors": {}}, status=410)
    if password != password2:
        return render(
            request,
            "auth/reset.html",
            {"valid": True, "errors": {"password2": "The two passwords do not match."}},
            status=400,
        )
    try:
        accounts.reset_password(db, token, password)
    except accounts.AccountError as exc:
        return render(
            request,
            "auth/reset.html",
            {"valid": True, "errors": {"password": str(exc)}},
            status=400,
        )
    db.commit()
    return RedirectResponse("/login?reset=1", status_code=303)
