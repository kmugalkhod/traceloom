from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from traceloom.db.models import AuditLog


def record(
    db: Session,
    *,
    action: str,
    actor_type: str,
    actor_id: str | None,
    org_id: str | None = None,
    project_id: str | None = None,
    target: str = "",
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    """Add an audit row to the caller's transaction (committed with the change it describes).

    `detail` must never carry secrets or passwords.
    """
    row = AuditLog(
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        org_id=org_id,
        project_id=project_id,
        target=target,
        detail=detail or {},
    )
    db.add(row)
    db.flush()
    return row
