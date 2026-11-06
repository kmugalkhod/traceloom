"""The public, read-only page for a trace that an Admin has shared. No login, no nav."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from traceloom.auth.deps import get_container
from traceloom.container import Container
from traceloom.ports import ObservationRow
from traceloom.web.render import render_value
from traceloom.web.templating import render
from traceloom.web.tree import build_tree, duration_ms, flatten

router = APIRouter(tags=["share"])

MAX_INDENT = 5


def _row(node, depth_cap: int = MAX_INDENT) -> dict[str, Any]:
    obs: ObservationRow = node.obs
    return {
        "obs": obs,
        "depth": min(node.depth, depth_cap),
        "duration_ms": duration_ms(obs),
        "cost": (obs.cost_details or {}).get("total"),
        "input": render_value(obs.input),
        "output": render_value(obs.output),
        "metadata": render_value(obs.metadata or None),
    }


@router.get("/share/{project_id}/{trace_id}", response_class=HTMLResponse)
def shared_trace(
    request: Request,
    project_id: str,
    trace_id: str,
    container: Container = Depends(get_container),
):
    detail = container.trace_store.get_trace(project_id, trace_id)
    if detail is None or not detail.trace.public:
        raise HTTPException(404, "Not found.")
    trace = detail.trace
    context = {
        "trace": trace,
        "rows": [_row(n) for n in flatten(build_tree(detail.observations))],
        "input": render_value(trace.input),
        "output": render_value(trace.output),
        "metadata": render_value(trace.metadata or None),
    }
    # Shared pages are for people with the link only: keep them out of search and caches.
    headers = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"}
    return render(request, "share.html", context, headers=headers)
