"""Builds the context the app shell needs from the user's memberships.

Only the models are used (no services), so any router can call it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceloom.auth.permissions import Perm, Role, can
from traceloom.db.models import Membership, Organization, Project, User

NAV = (
    ("dashboard", "Dashboard", "i-dash", "dashboard"),
    ("traces", "Traces", "i-traces", "traces"),
    ("sessions", "Sessions", "i-sessions", "sessions"),
    ("users", "Users", "i-users", "users"),
)


BUILD_NAV = (
    ("prompts", "Prompts", "i-prompt", "prompts"),
    ("playground", "Playground", "i-play", "playground"),
)
# Phone bottom bar: four destinations and a "More" sheet holding the rest.
BOTTOM_NAV = ("traces", "prompts", "playground", "dashboard")
MORE_NAV = ("sessions", "users")


def initials(text: str) -> str:
    parts = [p for p in text.replace("@", " ").replace(".", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def avatar_class(seed: str) -> str:
    """One of five pastel pairs, stable for a given user."""
    return f"av{sum(map(ord, seed)) % 5 + 1}"


@dataclass
class Shell:
    user: User
    orgs: list[dict[str, Any]]
    org: Organization | None
    project: Project | None
    role: Role | None
    active: str | None

    def context(self) -> dict[str, Any]:
        project_id = self.project.id if self.project else None
        base = f"/project/{project_id}" if project_id else None
        can_fn: Callable[[Perm | str], bool] = lambda perm: can(self.role, Perm(perm))  # noqa: E731

        def link(key: str, label: str, icon: str, path: str) -> dict[str, Any]:
            return {
                "key": key,
                "label": label,
                "icon": icon,
                "href": f"{base}/{path}" if base else "/orgs/new",
                "current": self.active == key,
            }

        everything = {
            key: link(key, label, icon, path) for key, label, icon, path in NAV + BUILD_NAV
        }
        return {
            "shell": True,
            "user": {
                "id": self.user.id,
                "name": self.user.name or self.user.email,
                "email": self.user.email,
                "initials": initials(self.user.name or self.user.email),
                "av": avatar_class(self.user.email),
            },
            "orgs": self.orgs,
            "org": self.org,
            "project": self.project,
            "role": self.role,
            "role_label": self.role.name.title() if self.role else "",
            "can": can_fn,
            "active": self.active,
            "nav": [link(*item) for item in NAV],
            "build_nav": [link(*item) for item in BUILD_NAV],
            "bottom_nav": [everything[key] for key in BOTTOM_NAV],
            "more_nav": [everything[key] for key in MORE_NAV],
            "settings": {
                "href": f"{base}/settings" if base else "/orgs/new",
                "current": self.active == "settings",
            },
        }


def build_shell(
    db: Session,
    user: User,
    *,
    org_id: str | None = None,
    project_id: str | None = None,
    active: str | None = None,
) -> Shell:
    """Orgs and projects the user belongs to, and which of them is current.

    The current project wins; then the org (first project in it); then the user's first org.
    """
    rows = db.execute(
        select(Organization, Membership.role)
        .join(Membership, Membership.org_id == Organization.id)
        .where(Membership.user_id == user.id)
        .order_by(Organization.name, Organization.id)
    ).all()
    org_ids = [org.id for org, _ in rows]
    projects_by_org: dict[str, list[Project]] = {oid: [] for oid in org_ids}
    if org_ids:
        for project in db.scalars(
            select(Project).where(Project.org_id.in_(org_ids)).order_by(Project.name, Project.id)
        ):
            projects_by_org[project.org_id].append(project)

    orgs = [
        {
            "id": org.id,
            "name": org.name,
            "role": Role.parse(role),
            "role_label": role.title(),
            "projects": projects_by_org[org.id],
        }
        for org, role in rows
    ]
    current_org = next(
        (o for o in orgs if project_id and any(p.id == project_id for p in o["projects"])), None
    )
    current_org = (
        current_org
        or next((o for o in orgs if o["id"] == org_id), None)
        or (orgs[0] if orgs else None)
    )
    project = None
    if current_org:
        projects = current_org["projects"]
        project = next((p for p in projects if p.id == project_id), None) or (
            projects[0] if projects else None
        )
    org_obj = next((org for org, _ in rows if current_org and org.id == current_org["id"]), None)
    return Shell(
        user=user,
        orgs=orgs,
        org=org_obj,
        project=project,
        role=current_org["role"] if current_org else None,
        active=active,
    )


def page_context(
    db: Session, access: Any, *, active: str | None = "settings", **extra: Any
) -> dict[str, Any]:
    """Shell context for a page resolved through auth.deps.require() (an `Access`)."""
    shell = build_shell(
        db,
        access.user,
        org_id=access.org.id,
        project_id=access.project.id if access.project else None,
        active=active,
    )
    return {**shell.context(), **extra}


def settings_tabs(project_id: str, current: str, role: Role | None) -> list[dict[str, Any]]:
    """The Settings tab bar. Tabs a role cannot use are not rendered."""
    base = f"/project/{project_id}/settings"
    tabs = [("general", "General", base), ("members", "Members & invites", f"{base}/members")]
    if can(role, Perm.API_KEYS):
        tabs.append(("keys", "API keys", f"{base}/keys"))
    if can(role, Perm.MODEL_DEFINITIONS):
        tabs.append(("models", "Models", f"{base}/models"))
    if can(role, Perm.WEBHOOKS):
        tabs.append(("webhooks", "Webhooks", f"{base}/webhooks"))
    if can(role, Perm.PROJECT_SETTINGS):
        tabs.append(("audit", "Audit log", f"{base}/audit"))
    return [
        {"key": k, "label": label, "href": href, "current": k == current} for k, label, href in tabs
    ]


def landing(project_id: str) -> str:
    """Where a signed-in user starts inside a project."""
    return f"/project/{project_id}/traces"


def env_context(
    db: Session, store: Any, user: User, project_id: str, selected: str | None
) -> dict[str, Any]:
    """What `_env_switcher.html` needs: the project's environments and the current choice.

    `selected` is the environment in effect (None = all). Pages pass what they resolved from the
    URL and the user's saved preference, so every list shows the same switcher.
    """
    from traceloom.services import views

    environments = store.list_environments(project_id)
    saved = views.get_pref(db, user.id, project_id, "env")
    if selected and selected not in environments:
        environments = sorted({*environments, selected})
    return {"env_ctx": {"options": environments, "current": selected, "saved": saved}}
