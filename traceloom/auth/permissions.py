"""Role x permission matrix (spec: Design -> Permissions)."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Role(IntEnum):
    VIEWER = 1
    MEMBER = 2
    ADMIN = 3
    OWNER = 4

    @classmethod
    def parse(cls, value: str) -> Role:
        return cls[value.upper()]

    @property
    def label(self) -> str:
        return self.name.lower()


class Perm(StrEnum):
    # Viewer+
    TRACES_READ = "traces:read"  # traces, sessions, users, dashboard, comments
    # Member+
    ANNOTATE = "traces:annotate"  # score, comment, bookmark, edit tags, own saved views
    # Admin+
    SHARE_TOGGLE = "traces:share"
    TRACES_DELETE = "traces:delete"
    COMMENT_DELETE_ANY = "comments:delete_any"
    PROJECT_SETTINGS = "project:settings"
    API_KEYS = "project:api_keys"
    MODEL_DEFINITIONS = "project:models"
    INVITES = "org:invites"
    ROLES = "org:roles"
    PROJECT_CREATE = "org:project_create"
    # Owner only
    OWNER_GRANT = "org:owner_grant"
    ORG_DELETE = "org:delete"


MIN_ROLE: dict[Perm, Role] = {
    Perm.TRACES_READ: Role.VIEWER,
    Perm.ANNOTATE: Role.MEMBER,
    Perm.SHARE_TOGGLE: Role.ADMIN,
    Perm.TRACES_DELETE: Role.ADMIN,
    Perm.COMMENT_DELETE_ANY: Role.ADMIN,
    Perm.PROJECT_SETTINGS: Role.ADMIN,
    Perm.API_KEYS: Role.ADMIN,
    Perm.MODEL_DEFINITIONS: Role.ADMIN,
    Perm.INVITES: Role.ADMIN,
    Perm.ROLES: Role.ADMIN,
    Perm.PROJECT_CREATE: Role.ADMIN,
    Perm.OWNER_GRANT: Role.OWNER,
    Perm.ORG_DELETE: Role.OWNER,
}


def can(role: Role | None, perm: Perm) -> bool:
    return role is not None and role >= MIN_ROLE[perm]
