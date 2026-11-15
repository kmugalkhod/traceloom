"""Public feedback (FR-S4): thumbs and ratings from end users' browsers, under a public key.

Only configs with `public_feedback` accept it. The score is an `API` score on the
`public_feedback` channel; sending the same `id` again replaces it, but never a score that did
not come from this channel. Allowed origins live in `project_eval_settings.feedback_origins`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceloom.db.models import ScoreConfig
from traceloom.db.models_eval import ProjectEvalSettings
from traceloom.ports import ScoreRow, TraceStore
from traceloom.scores import service
from traceloom.scores.configs import ConfigSnapshot, check_score

CHANNEL = "public_feedback"
COMMENT_MAX = 1000
MAX_ORIGINS = 50


class FeedbackError(ValueError):
    """A feedback request that cannot be saved; the message is safe to show to the caller."""


def normalize_origin(raw: str) -> str:
    """`scheme://host[:port]` in lower case. Anything with a path, query or credentials is
    refused, so an allowlist entry means exactly one origin."""
    text = (raw or "").strip()
    parts = urlsplit(text)
    try:
        port = parts.port
    except ValueError:
        raise FeedbackError(f"{text!r} is not a valid origin.") from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise FeedbackError(
            f"{text!r} is not an origin: use scheme and host, like https://app.example.com."
        )
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def _row(db: Session, project_id: str) -> ProjectEvalSettings | None:
    return db.get(ProjectEvalSettings, project_id)


def feedback_origins(db: Session, project_id: str) -> list[str]:
    row = _row(db, project_id)
    return list(row.feedback_origins or []) if row else []


def all_origins(db: Session) -> set[str]:
    """The union of every project's allowlist (what a preflight, which carries no key, can use)."""
    out: set[str] = set()
    for origins in db.scalars(select(ProjectEvalSettings.feedback_origins)):
        out.update(origins or [])
    return out


def set_feedback_origins(db: Session, project_id: str, origins: list[str]) -> list[str]:
    """Replace the allowlist (validated, normalised, de-duplicated). The caller commits."""
    cleaned = list(dict.fromkeys(normalize_origin(o) for o in origins if o.strip()))
    if len(cleaned) > MAX_ORIGINS:
        raise FeedbackError(f"At most {MAX_ORIGINS} origins can be allowed.")
    row = _row(db, project_id)
    if row is None:
        row = ProjectEvalSettings(project_id=project_id, feedback_origins=[])
        db.add(row)
    row.feedback_origins = cleaned
    db.flush()
    return cleaned


def find_config(
    db: Session, project_id: str, *, name: str | None, config_id: str | None
) -> ScoreConfig:
    query = select(ScoreConfig).where(
        ScoreConfig.project_id == project_id, ScoreConfig.archived_at.is_(None)
    )
    if config_id:
        query = query.where(ScoreConfig.id == config_id)
    elif name:
        query = query.where(ScoreConfig.name == name)
    else:
        raise FeedbackError("Name the score config (name or configId).")
    config = db.scalar(query)
    if config is None or not config.public_feedback:
        raise FeedbackError("This score config does not accept public feedback.")
    return config


def submit(
    db: Session,
    store: TraceStore,
    project_id: str,
    *,
    name: str | None,
    config_id: str | None,
    value: Any,
    trace_id: str | None,
    observation_id: str | None,
    session_id: str | None,
    comment: str | None,
    score_id: str | None,
) -> ScoreRow:
    if not (trace_id or session_id):
        raise FeedbackError("A feedback needs a traceId or a sessionId.")
    if comment is not None and len(comment) > COMMENT_MAX:
        raise FeedbackError(f"The comment can be at most {COMMENT_MAX} characters.")
    config = find_config(db, project_id, name=name, config_id=config_id)
    problem = check_score(ConfigSnapshot.of(config), value)
    if problem:
        raise FeedbackError(problem)
    if score_id:
        existing = store.get_existing(project_id, {"score": [score_id]}).get("score", {})
        if score_id in existing and existing[score_id].channel != CHANNEL:
            raise FeedbackError("That id belongs to another score.")
    try:
        return service.create_score(
            store,
            project_id,
            name=config.name,
            value=bool(value) if config.data_type == "BOOLEAN" else value,
            source="API",
            config_id=config.id,
            data_type=config.data_type,
            score_id=score_id,
            trace_id=trace_id,
            observation_id=observation_id,
            session_id=session_id,
            comment=comment or None,
            channel=CHANNEL,
        )
    except service.ScoreError as exc:
        raise FeedbackError(str(exc)) from exc
