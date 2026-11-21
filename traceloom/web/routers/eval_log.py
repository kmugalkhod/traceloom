"""Evaluation > Evaluators > Log: every judge execution with its status, filters by rule, status
and time, a details panel, and Retry for failed executions (Member+) (FR-J8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from traceloom.auth.deps import Access, get_container, require
from traceloom.auth.permissions import Perm, can
from traceloom.container import Container
from traceloom.db.models_eval import (
    EXECUTION_STATUSES,
    EvalExecution,
    EvalRule,
    EvalTemplate,
    EvalTemplateVersion,
)
from traceloom.db.session import get_db
from traceloom.evals import runner
from traceloom.ports import ScoreRow
from traceloom.services import audit
from traceloom.web.routers.evaluators import shell
from traceloom.web.templating import render

router = APIRouter(tags=["eval-log"])

RANGES = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)}
DEFAULT_RANGE = "24h"
PAGE = 100
STATUS_LABELS = {
    "succeeded": "Succeeded",
    "failed": "Failed",
    "capped": "Capped",
    "skipped": "Skipped",
    "running": "Running",
    "pending": "Pending",
}
NOTES = {"retried": "Execution queued for another attempt."}
# Retrying re-runs a judge on the same data, like saving a template: Member and above.
RETRY_PERM = Perm.EVAL_TEMPLATES_WRITE


def _base(access: Access) -> str:
    return f"/project/{access.project.id}/evals/log"


def _score_text(score: ScoreRow | None) -> str:
    if score is None:
        return ""
    if score.data_type == "CATEGORICAL":
        return score.string_value or ""
    if score.data_type == "BOOLEAN":
        return "true" if score.value else "false"
    return f"{score.value:g}" if score.value is not None else ""


def _duration(ex: EvalExecution) -> float | None:
    if ex.started_at and ex.finished_at:
        return max((ex.finished_at - ex.started_at).total_seconds(), 0.0)
    return None


def _query(access: Access, rule_id: str, since: datetime):
    query = select(EvalExecution).where(
        EvalExecution.project_id == access.project.id, EvalExecution.created_at >= since
    )
    return query.where(EvalExecution.rule_id == rule_id) if rule_id else query


@router.get("/project/{project_id}/evals/log", response_class=HTMLResponse)
def log_page(
    request: Request,
    rule: str = "",
    status: str = "",
    range: str = DEFAULT_RANGE,  # noqa: A002 - the query parameter's name
    open: str = "",  # noqa: A002
    notice: str = "",
    access: Access = Depends(require(Perm.EVAL_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    project_id = access.project.id
    range_key = range if range in RANGES else DEFAULT_RANGE
    status = status if status in EXECUTION_STATUSES else ""
    since = datetime.now(UTC) - RANGES[range_key]
    rules = {r.id: r for r in db.scalars(select(EvalRule).where(EvalRule.project_id == project_id))}
    rule = rule if rule in rules else ""

    base = _query(access, rule, since)
    counts = dict(
        db.execute(
            select(EvalExecution.status, func.count())
            .where(EvalExecution.id.in_(base.with_only_columns(EvalExecution.id)))
            .group_by(EvalExecution.status)
        ).all()
    )
    shown = base.where(EvalExecution.status == status) if status else base
    executions = db.scalars(shown.order_by(EvalExecution.created_at.desc()).limit(PAGE)).all()

    versions = {
        v.id: v
        for v in db.scalars(
            select(EvalTemplateVersion).where(
                EvalTemplateVersion.id.in_({e.template_version_id for e in executions})
            )
        )
    }
    templates = {
        t.id: t
        for t in db.scalars(
            select(EvalTemplate).where(
                EvalTemplate.id.in_({v.template_id for v in versions.values()})
            )
        )
    }
    rows = [_row(container, access, ex, rules, versions, templates) for ex in executions]
    opened = next((r for r in rows if r["id"] == open), None)
    if opened is None and open:
        opened = _row_for(container, access, db, open, rules)
    context = shell(
        db,
        access,
        "log",
        rows=rows,
        rule_options=sorted(
            ({"id": r.id, "title": r.name or r.score_name} for r in rules.values()),
            key=lambda r: r["title"],
        ),
        statuses=[
            {"key": key, "label": label, "count": counts.get(key, 0)}
            for key, label in STATUS_LABELS.items()
        ],
        total=sum(counts.values()),
        rule=rule,
        status=status,
        range=range_key,
        ranges=list(RANGES),
        opened=opened,
        can_retry=can(access.role, RETRY_PERM),
        notice=NOTES.get(notice, ""),
        limit=PAGE,
        link=lambda **kw: _link(access, {"rule": rule, "status": status, "range": range_key, **kw}),
        max_attempts=container.settings.eval_max_attempts,
    )
    return render(request, "evals/log.html", context)


def _link(access: Access, params: dict[str, str]) -> str:
    keep = {k: v for k, v in params.items() if v and not (k == "range" and v == DEFAULT_RANGE)}
    return _base(access) + (f"?{urlencode(keep)}" if keep else "")


def _row(
    container: Container,
    access: Access,
    ex: EvalExecution,
    rules: dict[str, EvalRule],
    versions: dict[str, EvalTemplateVersion],
    templates: dict[str, EvalTemplate],
) -> dict[str, Any]:
    rule = rules.get(ex.rule_id or "")
    version = versions.get(ex.template_version_id)
    template = templates.get(version.template_id) if version else None
    score: ScoreRow | None = None
    if ex.status == "succeeded" and ex.score_id and ex.trace_id:
        detail = container.trace_store.get_trace(access.project.id, ex.trace_id)
        score = next((s for s in detail.scores if s.id == ex.score_id), None) if detail else None
    when = ex.created_at
    return {
        "id": ex.id,
        "when": when,
        "rule": (rule.name or rule.score_name) if rule else "Experiment evaluator",
        "template": f"{template.name} v{version.version}" if template and version else "",
        "target_type": ex.object_type,
        "target_id": ex.object_id,
        "trace_id": ex.trace_id,
        "status": ex.status,
        "status_label": STATUS_LABELS.get(ex.status, ex.status),
        "attempts": ex.attempts,
        "score": score,
        "score_name": score.name if score else "",
        "score_value": _score_text(score),
        "reasoning": score.comment if score else None,
        "judge_trace_id": ex.judge_trace_id,
        "cost": ex.cost,
        "duration": _duration(ex),
        "error": ex.error,
        "run_after": ex.run_after,
    }


def _row_for(container, access, db, execution_id, rules) -> dict[str, Any] | None:
    ex = db.scalar(
        select(EvalExecution).where(
            EvalExecution.id == execution_id, EvalExecution.project_id == access.project.id
        )
    )
    if ex is None:
        return None
    version = db.get(EvalTemplateVersion, ex.template_version_id)
    template = db.get(EvalTemplate, version.template_id) if version else None
    return _row(
        container,
        access,
        ex,
        rules,
        {version.id: version} if version else {},
        {template.id: template} if template else {},
    )


@router.post("/project/{project_id}/evals/log/{execution_id}/retry")
def retry(
    execution_id: str,
    access: Access = Depends(require(RETRY_PERM)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    if not runner.retry_execution(container, access.project.id, execution_id):
        raise HTTPException(409, "Only a failed or capped execution can be retried.")
    audit.record(
        db,
        action="eval.execution.retry",
        actor_type="user",
        actor_id=access.user.id,
        org_id=access.project.org_id,
        project_id=access.project.id,
        target=execution_id,
        detail={},
    )
    db.commit()
    return RedirectResponse(f"{_base(access)}?notice=retried&open={execution_id}", status_code=303)
