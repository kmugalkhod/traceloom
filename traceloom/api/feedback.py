"""POST /api/public/feedback: the one endpoint a public key (`X-Traceloom-Public-Key`) can use.

Browsers send it from end users' pages, so it has its own CORS rules (ADR-0015):
- the preflight carries no key, so it answers from the union of every project's allowed origins;
- the POST is checked against the key's own project: a request whose `Origin` is not in that
  list gets 403 and no CORS headers. A request without an `Origin` (curl, a server) is allowed,
  since the public key is not a secret and only browsers enforce CORS;
- limits per key and per client IP (`TRACELOOM_RATE_FEEDBACK`), 429 with Retry-After beyond.
Success is 202 with the score id; sending the same `id` again replaces the score.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StringConstraints
from pydantic.alias_generators import to_camel
from sqlalchemy.orm import Session

from traceloom.api.ingestion import json_body
from traceloom.auth.deps import FeedbackPrincipal, feedback_key, get_container
from traceloom.container import Container
from traceloom.db.session import get_db
from traceloom.scores import feedback

router = APIRouter(prefix="/api/public", tags=["feedback"])

MAX_BODY_BYTES = 20_000
Id = Annotated[str, StringConstraints(min_length=1, max_length=200)]
ALLOW_HEADERS = "Content-Type, X-Traceloom-Public-Key"


class FeedbackBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    id: Id | None = None
    trace_id: Id | None = None
    observation_id: Id | None = None
    session_id: Id | None = None
    name: Id | None = None
    config_id: Id | None = None
    value: float | str | bool
    comment: str | None = None


def _cors(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Expose-Headers": "Retry-After",
        "Vary": "Origin",
    }


def _origin_of(request: Request) -> str | None:
    raw = request.headers.get("origin")
    if raw is None:
        return None
    try:
        return feedback.normalize_origin(raw)
    except feedback.FeedbackError:
        return raw  # "null" and the like never match an allowlist entry


@router.options("/feedback")
def preflight(request: Request, db: Session = Depends(get_db)) -> Response:
    origin = _origin_of(request)
    headers: dict[str, str] = {}
    if origin and origin in feedback.all_origins(db):
        headers = {
            **_cors(origin),
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": ALLOW_HEADERS,
            "Access-Control-Max-Age": "600",
        }
    return Response(status_code=204, headers=headers)


def _limit(container: Container, scope: str, ident: str, headers: dict[str, str]) -> None:
    result = container.limiter.hit(f"feedback:{scope}:{ident}", container.settings.rate_feedback)
    if not result.allowed:
        raise HTTPException(
            429,
            "Rate limit exceeded.",
            headers={**headers, "Retry-After": str(max(1, round(result.retry_after)))},
        )


@router.post("/feedback", status_code=202)
def post_feedback(
    request: Request,
    response: Response,
    principal: FeedbackPrincipal = Depends(feedback_key),  # first: auth before the body is read
    body: FeedbackBody = Depends(json_body(FeedbackBody, MAX_BODY_BYTES)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    origin = _origin_of(request)
    headers: dict[str, str] = {}
    if origin is not None:
        if origin not in feedback.feedback_origins(db, principal.project_id):
            raise HTTPException(403, "This origin is not allowed to send feedback.")
        headers = _cors(origin)
    ip = request.client.host if request.client else "unknown"
    _limit(container, "key", principal.key_id, headers)
    _limit(container, "ip", ip, headers)
    try:
        row = feedback.submit(
            db,
            container.trace_store,
            principal.project_id,
            name=body.name,
            config_id=body.config_id,
            value=body.value,
            trace_id=body.trace_id,
            observation_id=body.observation_id,
            session_id=body.session_id,
            comment=body.comment,
            score_id=body.id,
        )
    except feedback.FeedbackError as exc:
        raise HTTPException(400, str(exc), headers=headers) from exc
    response.headers.update(headers)
    return {"id": row.id}
