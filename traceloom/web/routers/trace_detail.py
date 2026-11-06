"""The trace detail page and its IO pane (HTMX partial)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from traceloom.auth.deps import Access, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.session import get_db
from traceloom.web import trace_detail as td
from traceloom.web import tree
from traceloom.web.context import page_context
from traceloom.web.formatting import count, latency, money
from traceloom.web.templating import render

router = APIRouter(tags=["trace-detail"])


def _load(container: Container, project_id: str, trace_id: str):
    detail = container.trace_store.get_trace(project_id, trace_id)
    if detail is None:
        raise HTTPException(404, f"There is no trace {trace_id} in this project.")
    return detail


@router.get("/project/{project_id}/traces/{trace_id}", response_class=HTMLResponse)
def trace_page(
    request: Request,
    trace_id: str,
    access: Access = Depends(require(Perm.TRACES_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    project_id = access.project.id
    detail = _load(container, project_id, trace_id)
    state = td.parse_state(project_id, trace_id, request.query_params)
    all_obs = detail.observations
    visible = tree.filter_debug(all_obs, state.debug)
    roots = tree.build_tree(visible)
    tl = tree.timeline(visible)
    show_graph = td.has_agent_steps(visible)
    graph = tree.agent_graph(visible) if show_graph else None
    first_error = tree.first_error(visible)
    if state.view == "graph" and not show_graph:
        state.view = "tree"
    if state.obs and state.obs not in {o.id for o in all_obs}:
        state.obs = None  # a stale link falls back to the trace itself
    trace = detail.trace
    flags = [
        name for name, on in (("Bookmarked", trace.bookmarked), ("Public", trace.public)) if on
    ]
    context = page_context(
        db,
        access,
        active="traces",
        detail=detail,
        trace=trace,
        state=state,
        flags=flags,
        nodes=td.node_rows(roots, state),
        timeline=tl,
        timeline_rows=td.timeline_rows(tl, state),
        graph=graph,
        node_urls={
            n.first_observation_id: {
                "page": state.page_url(obs=n.first_observation_id),
                "io": state.io_url(obs=n.first_observation_id),
            }
            for n in (graph.nodes if graph else [])
        },
        edge_path=td.edge_path,
        show_graph=show_graph,
        first_error_id=first_error.id if first_error else None,
        first_error_url=state.page_url(obs=first_error.id) if first_error else None,
        first_error_io=state.io_url(obs=first_error.id) if first_error else None,
        debug_count=sum(1 for o in all_obs if o.level == "DEBUG"),
        debug_url=state.page_url(debug=0 if state.debug else 1),
        pane=td.io_pane(detail, state),
        kv={
            "latency": latency(trace.latency_ms),
            "cost": money(trace.total_cost),
            "tokens": count(trace.total_tokens),
        },
        list_url=f"/project/{project_id}/traces",
        scores=detail.scores,
    )
    return render(request, "traces/detail.html", context)


@router.get("/project/{project_id}/traces/{trace_id}/io", response_class=HTMLResponse)
def io_partial(
    request: Request,
    trace_id: str,
    access: Access = Depends(require(Perm.TRACES_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    project_id = access.project.id
    detail = _load(container, project_id, trace_id)
    state = td.parse_state(project_id, trace_id, request.query_params)
    if state.obs and state.obs not in {o.id for o in detail.observations}:
        raise HTTPException(404, "There is no such observation in this trace.")
    context = page_context(
        db,
        access,
        active="traces",
        state=state,
        pane=td.io_pane(detail, state),
        scores=detail.scores,
        trace=detail.trace,
    )
    return render(request, "traces/_io.html", context)
