"""Saved views (per project, shared with all members) and per-user display preferences."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceloom.auth.permissions import Perm, Role, can
from traceloom.db.models import SavedView, User, UserPref
from traceloom.services.orgs import NotAllowed, OrgError

MAX_NAME = 120
MAX_QUERY = 2000
# Paging and per-user state never belong in a saved view.
DROPPED_PARAMS = {"cursor"}


def clean_query(query: str) -> str:
    """The filters of a trace list URL, without a leading '?' and without paging."""
    pairs = [
        (k, v)
        for k, v in parse_qsl(query.lstrip("?"), keep_blank_values=False)
        if k not in DROPPED_PARAMS
    ]
    return urlencode(pairs)


def _checked_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise OrgError("Give the view a name.")
    if len(name) > MAX_NAME:
        raise OrgError(f"The name must be {MAX_NAME} characters or fewer.")
    return name


def _checked_query(query: str) -> str:
    query = clean_query(query)
    if len(query) > MAX_QUERY:
        raise OrgError("These filters are too long to save.")
    return query


def list_views(db: Session, project_id: str) -> list[tuple[SavedView, str]]:
    """(view, creator name) for the project, by name. Everyone in the project can apply them."""
    rows = db.execute(
        select(SavedView, User.name, User.email)
        .join(User, User.id == SavedView.owner_id)
        .where(SavedView.project_id == project_id)
        .order_by(SavedView.name, SavedView.id)
    ).all()
    return [(view, name or email) for view, name, email in rows]


def can_edit(view: SavedView, user: User, role: Role) -> bool:
    return view.owner_id == user.id or can(role, Perm.SHARE_TOGGLE)  # Admin and Owner


def create_view(
    db: Session, project_id: str, user: User, role: Role, name: str, query: str
) -> SavedView:
    if not can(role, Perm.ANNOTATE):
        raise NotAllowed("Only members can save views.")
    view = SavedView(
        project_id=project_id,
        owner_id=user.id,
        name=_checked_name(name),
        query=_checked_query(query),
    )
    db.add(view)
    db.flush()
    return view


def _get(db: Session, project_id: str, view_id: str) -> SavedView:
    view = db.get(SavedView, view_id)
    if view is None or view.project_id != project_id:
        raise OrgError("That view no longer exists.")
    return view


def update_view(
    db: Session,
    project_id: str,
    user: User,
    role: Role,
    view_id: str,
    name: str | None,
    query: str | None,
) -> SavedView:
    view = _get(db, project_id, view_id)
    if not can_edit(view, user, role):
        raise NotAllowed("Only the creator or an Admin can edit this view.")
    if name is not None:
        view.name = _checked_name(name)
    if query is not None:
        view.query = _checked_query(query)
    db.flush()
    return view


def delete_view(db: Session, project_id: str, user: User, role: Role, view_id: str) -> None:
    view = _get(db, project_id, view_id)
    if not can_edit(view, user, role):
        raise NotAllowed("Only the creator or an Admin can delete this view.")
    db.delete(view)
    db.flush()


# --- user preferences (column choice, environment) -------------------------------------------


def get_pref(db: Session, user_id: str, project_id: str, key: str) -> str | None:
    pref = db.get(UserPref, (user_id, project_id, key))
    return pref.value if pref else None


def set_pref(db: Session, user_id: str, project_id: str, key: str, value: str) -> None:
    pref = db.get(UserPref, (user_id, project_id, key))
    if pref is None:
        db.add(UserPref(user_id=user_id, project_id=project_id, key=key, value=value))
    else:
        pref.value = value
    db.flush()


def clear_pref(db: Session, user_id: str, project_id: str, key: str) -> None:
    pref = db.get(UserPref, (user_id, project_id, key))
    if pref is not None:
        db.delete(pref)
        db.flush()
