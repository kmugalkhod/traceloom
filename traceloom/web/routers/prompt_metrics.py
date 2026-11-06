"""Per-version prompt metrics (FR-P8): the partial the detail page's Metrics tab loads."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from traceloom.auth.deps import Access, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.session import get_db
from traceloom.ports import PromptMetricsQuery
from traceloom.prompts import service
from traceloom.prompts.service import Actor, PromptNotFound
from traceloom.web.envpref import resolve_env, saved_env
from traceloom.web.prompt_paths import prompt_url
from traceloom.web.ranges import resolve_range
from traceloom.web.templating import render

router = APIRouter(tags=["prompts"])

GENERATIONS = 50
RANGE_LABEL = {
    "1h": "last hour",
    "24h": "last 24 hours",
    "7d": "last 7 days",
    "30d": "last 30 days",
}


@router.get("/project/{project_id}/prompts/{name:path}/metrics", response_class=HTMLResponse)
def prompt_metrics(
    request: Request,
    name: str,
    version: str = "",
    access: Access = Depends(require(Perm.PROMPTS_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    project = access.project
    actor = Actor.for_user(access.user, access.role)
    try:  # project-scoped: another project's prompt does not exist here
        head = service.get(db, project, actor, name, label=service.LATEST, resolve=False)
    except PromptNotFound as exc:
        raise HTTPException(404, "Prompt not found.") from exc

    all_versions = version == "all"
    number = None if all_versions else _version(version, head.latest_version)
    if number is not None and not 1 <= number <= head.latest_version:
        raise HTTPException(404, "Version not found.")

    params = request.query_params
    range_key, start, end = resolve_range(params)
    env = resolve_env(params, saved_env(db, access.user.id, project.id))
    query = PromptMetricsQuery(from_time=start, to_time=end, environment=env)
    store = container.trace_store
    metrics = store.prompt_metrics(project.id, name, number, query)
    generations = (
        store.list_prompt_generations(project.id, name, number, GENERATIONS, query)
        if metrics.count
        else []
    )
    labels = service.prompt_labels(db, project, head.prompt_id)

    scope = "all versions" if all_versions else f"v{number}"
    context = {
        "project": project,
        "name": name,
        "metrics": metrics,
        "generations": generations,
        "per_call": (metrics.total_cost / metrics.count) if metrics.count else None,
        "scope": f"{scope}, {RANGE_LABEL[range_key]}" + (f", {env}" if env else ""),
        "version": "all" if all_versions else number,
        "latest": head.latest_version,
        "versions": list(range(head.latest_version, 0, -1)),
        "production": labels.get(service.PRODUCTION_LABEL),
        "range_label": RANGE_LABEL[range_key],
        "env": env,
        "limit": GENERATIONS,
        "metrics_url": prompt_url(project.id, name, "/metrics"),
        "range_key": range_key,
        "new_url": f"/project/{project.id}/prompts/new?" + urlencode({"from": name}),
        "can_write": actor.can(Perm.PROMPTS_WRITE),
        "sdk_name": name,
    }
    return render(request, "prompts/_metrics.html", context)


def _version(raw: str, default: int) -> int:
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(400, "Version must be a number or `all`.") from None
