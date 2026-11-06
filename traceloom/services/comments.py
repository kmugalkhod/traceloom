"""Comments on traces and observations: validation, @mentions, deletion rules.

Markdown is rendered (sanitized) by the web layer; this module stores the raw text and never
produces HTML.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from traceloom.auth.permissions import Perm, Role, can
from traceloom.config import Settings
from traceloom.db.base import utcnow
from traceloom.db.models import Comment, Membership, Project, User
from traceloom.services.mail import Mailer

log = logging.getLogger("traceloom")

MAX_LENGTH = 5000
OBJECT_TYPES = ("trace", "observation")
_EMAIL = re.compile(r"@([A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+)")


class CommentError(ValueError):
    """The comment cannot be saved (message is safe to show)."""


class NotAllowed(PermissionError):
    pass


@dataclass(frozen=True)
class CommentView:
    id: str
    body: str
    created_at: datetime
    author_id: str
    author_name: str
    author_email: str
    object_type: str
    object_id: str


def project_members(db: Session, project: Project) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.org_id == project.org_id)
            .order_by(User.name, User.email)
        )
    )


def mentioned(body: str, members: list[User]) -> list[User]:
    """Members named by `@email` or `@Display Name` (case-insensitive, whole words)."""
    emails = {e.lower() for e in _EMAIL.findall(body)}
    lowered = body.lower()
    found = []
    for user in members:
        by_email = user.email.lower() in emails
        name = (user.name or "").strip().lower()
        by_name = bool(name) and re.search(rf"@{re.escape(name)}(?![\w])", lowered) is not None
        if by_email or by_name:
            found.append(user)
    return found


def add_comment(
    db: Session,
    *,
    project: Project,
    trace_id: str,
    author: User,
    body: str,
    object_type: str = "trace",
    object_id: str | None = None,
) -> Comment:
    body = (body or "").strip()
    if not body:
        raise CommentError("Write a comment first.")
    if len(body) > MAX_LENGTH:
        raise CommentError(f"Comments are limited to {MAX_LENGTH:,} characters.")
    if object_type not in OBJECT_TYPES:
        raise CommentError("Unknown comment target.")
    comment = Comment(
        project_id=project.id,
        object_type=object_type,
        object_id=(object_id or trace_id) if object_type == "observation" else trace_id,
        trace_id=trace_id,
        author_id=author.id,
        body=body,
        created_at=utcnow(),
    )
    db.add(comment)
    db.flush()
    return comment


def notify_mentions(
    db: Session,
    mailer: Mailer,
    settings: Settings,
    *,
    project: Project,
    trace_id: str,
    author: User,
    body: str,
) -> list[str]:
    """Email each mentioned project member (not the author). Returns the addresses mailed."""
    sent = []
    link = f"{settings.base_url.rstrip('/')}/project/{project.id}/traces/{trace_id}"
    who = author.name or author.email
    excerpt = body if len(body) <= 400 else body[:400] + "..."
    for user in mentioned(body, project_members(db, project)):
        if user.id == author.id:
            continue
        try:
            mailer.send(
                user.email,
                f"{who} mentioned you in {project.name}",
                f"{who} mentioned you in a comment on trace {trace_id}:\n\n{excerpt}\n\n{link}\n",
            )
            sent.append(user.email)
        except Exception:  # mail trouble must not lose the comment
            log.exception("mention mail to %s failed", user.email)
    return sent


def list_comments(db: Session, project_id: str, trace_id: str) -> list[CommentView]:
    rows = db.execute(
        select(Comment, User)
        .join(User, User.id == Comment.author_id)
        .where(Comment.project_id == project_id, Comment.trace_id == trace_id)
        .order_by(Comment.created_at, Comment.id)
    ).all()
    return [
        CommentView(
            c.id, c.body, c.created_at, u.id, u.name or u.email, u.email, c.object_type, c.object_id
        )
        for c, u in rows
    ]


def can_delete(role: Role | None, user_id: str, author_id: str) -> bool:
    return user_id == author_id or can(role, Perm.COMMENT_DELETE_ANY)


def delete_comment(
    db: Session, *, project_id: str, comment_id: str, user: User, role: Role | None
) -> bool:
    """Delete by the author or an Admin+. False when the comment does not exist here."""
    comment = db.scalar(
        select(Comment).where(Comment.project_id == project_id, Comment.id == comment_id)
    )
    if comment is None:
        return False
    if not can_delete(role, user.id, comment.author_id):
        raise NotAllowed("Only the author or an admin can delete this comment.")
    db.delete(comment)
    db.flush()
    return True


def delete_for_traces(db: Session, project_id: str, trace_ids: list[str]) -> int:
    """Remove every comment on the given traces (the meta DB side of a trace delete)."""
    deleted = 0
    for start in range(0, len(trace_ids), 500):
        chunk = trace_ids[start : start + 500]
        deleted += db.execute(
            delete(Comment).where(Comment.project_id == project_id, Comment.trace_id.in_(chunk))
        ).rowcount
    return deleted
