"""Organizations, projects and members (FR-A3, FR-A4).

Every function takes the acting user's role in the org and re-checks it, so the rules hold no
matter which route calls them: only an Owner grants Owner or deletes the org, an Admin can
manage members below Owner, and the last Owner can neither be demoted nor leave.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from traceloom.auth.permissions import Perm, Role, can
from traceloom.container import Container
from traceloom.db.models import (
    ApiKey,
    Membership,
    Observation,
    Organization,
    Project,
    Score,
    Trace,
    User,
)
from traceloom.services import audit


class OrgError(ValueError):
    """The request is valid but breaks a rule (shown to the user)."""


class NotAllowed(OrgError):
    """The acting role may not do this (HTTP 403)."""


def _name(value: str, label: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise OrgError(f"{label} is required.")
    if len(value) > 200:
        raise OrgError(f"{label} must be 200 characters or fewer.")
    return value


def create_org(
    db: Session, user: User, name: str, project_name: str
) -> tuple[Organization, Project]:
    """The creator becomes Owner and gets a first project."""
    org = Organization(name=_name(name, "Organization name"))
    db.add(org)
    db.flush()
    db.add(Membership(user_id=user.id, org_id=org.id, role=Role.OWNER.label))
    project = Project(org_id=org.id, name=_name(project_name, "Project name"))
    db.add(project)
    db.flush()
    audit.record(
        db,
        action="org.created",
        actor_type="user",
        actor_id=user.id,
        org_id=org.id,
        project_id=project.id,
        target=org.id,
        detail={"name": org.name},
    )
    return org, project


def create_project(db: Session, user: User, role: Role, org_id: str, name: str) -> Project:
    if not can(role, Perm.PROJECT_CREATE):
        raise NotAllowed("Only an Admin or Owner can create projects.")
    project = Project(org_id=org_id, name=_name(name, "Project name"))
    db.add(project)
    db.flush()
    audit.record(
        db,
        action="project.created",
        actor_type="user",
        actor_id=user.id,
        org_id=org_id,
        project_id=project.id,
        target=project.id,
        detail={"name": project.name},
    )
    return project


def rename_project(db: Session, user: User, role: Role, project: Project, name: str) -> Project:
    if not can(role, Perm.PROJECT_SETTINGS):
        raise NotAllowed("Only an Admin or Owner can change project settings.")
    project.name = _name(name, "Project name")
    db.flush()
    return project


def owner_count(db: Session, org_id: str) -> int:
    return db.scalar(
        select(func.count())
        .select_from(Membership)
        .where(Membership.org_id == org_id, Membership.role == Role.OWNER.label)
    )


@dataclass
class MemberRow:
    user: User
    role: Role
    is_self: bool
    last_owner: bool
    can_change_role: bool
    can_remove: bool
    can_leave: bool


def member_rows(db: Session, org_id: str, actor: User, actor_role: Role) -> list[MemberRow]:
    owners = owner_count(db, org_id)
    rows = db.execute(
        select(User, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.org_id == org_id)
        .order_by(User.name, User.email)
    ).all()
    out = []
    for user, role_text in rows:
        role = Role.parse(role_text)
        is_self = user.id == actor.id
        last_owner = role is Role.OWNER and owners <= 1
        manageable = can(actor_role, Perm.ROLES) and (
            role is not Role.OWNER or actor_role is Role.OWNER
        )
        out.append(
            MemberRow(
                user=user,
                role=role,
                is_self=is_self,
                last_owner=last_owner,
                can_change_role=manageable and not last_owner,
                can_remove=manageable and not is_self and not last_owner,
                can_leave=is_self and not last_owner,
            )
        )
    return out


def change_role(
    db: Session, actor: User, actor_role: Role, org_id: str, user_id: str, new_role: Role
) -> Membership:
    if not can(actor_role, Perm.ROLES):
        raise NotAllowed("Only an Admin or Owner can change roles.")
    membership = db.get(Membership, (user_id, org_id))
    if membership is None:
        raise OrgError("That person is not a member of this organization.")
    current = Role.parse(membership.role)
    if new_role is Role.OWNER and not can(actor_role, Perm.OWNER_GRANT):
        raise NotAllowed("Only an Owner can grant the Owner role.")
    if current is Role.OWNER and actor_role is not Role.OWNER:
        raise NotAllowed("Only an Owner can change an Owner's role.")
    if current is Role.OWNER and new_role is not Role.OWNER and owner_count(db, org_id) <= 1:
        raise OrgError("The last Owner cannot be demoted. Promote another member to Owner first.")
    if current is not new_role:
        membership.role = new_role.label
        audit.record(
            db,
            action="member.role_changed",
            actor_type="user",
            actor_id=actor.id,
            org_id=org_id,
            target=user_id,
            detail={"from": current.label, "to": new_role.label},
        )
    db.flush()
    return membership


def remove_member(db: Session, actor: User, actor_role: Role, org_id: str, user_id: str) -> None:
    """Remove a member, or let someone leave (removing yourself needs no role)."""
    leaving = user_id == actor.id
    if not leaving and not can(actor_role, Perm.ROLES):
        raise NotAllowed("Only an Admin or Owner can remove members.")
    membership = db.get(Membership, (user_id, org_id))
    if membership is None:
        raise OrgError("That person is not a member of this organization.")
    target_role = Role.parse(membership.role)
    if not leaving:
        if target_role is Role.OWNER and actor_role is not Role.OWNER:
            raise NotAllowed("Only an Owner can remove an Owner.")
    if target_role is Role.OWNER and owner_count(db, org_id) <= 1:
        raise OrgError(
            "The last Owner cannot leave or be removed. Promote another member to Owner first."
        )
    db.delete(membership)
    audit.record(
        db,
        action="member.left" if leaving else "member.removed",
        actor_type="user",
        actor_id=actor.id,
        org_id=org_id,
        target=user_id,
        detail={"role": target_role.label},
    )
    db.flush()


def delete_org(
    db: Session, container: Container, actor: User, actor_role: Role, org: Organization
) -> None:
    """Delete the org with its projects, keys, members and every trace (Owner only)."""
    if not can(actor_role, Perm.ORG_DELETE):
        raise NotAllowed("Only an Owner can delete the organization.")
    project_ids = list(db.scalars(select(Project.id).where(Project.org_id == org.id)))
    for key in db.scalars(select(ApiKey).where(ApiKey.project_id.in_(project_ids))):
        container.api_key_cache.evict(key.public_key)  # a deleted org's keys stop working at once
    if project_ids:  # trace tables have no foreign keys; the rest cascades from the org
        for table in (Score, Observation, Trace):
            db.execute(delete(table).where(table.project_id.in_(project_ids)))
    audit.record(
        db,
        action="org.deleted",
        actor_type="user",
        actor_id=actor.id,
        org_id=org.id,
        target=org.id,
        detail={"name": org.name, "projects": len(project_ids)},
    )
    db.delete(org)
    db.flush()
