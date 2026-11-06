"""The project dashboard (FR-D1): totals, cost and tokens by model, latency, errors, top users."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from traceloom.auth.deps import Access, get_container, require
from traceloom.auth.permissions import Perm
from traceloom.container import Container
from traceloom.db.session import get_db
from traceloom.ports import DashboardData, DashboardQuery
from traceloom.web import pagefmt  # noqa: F401  (registers the filters)
from traceloom.web.context import page_context
from traceloom.web.ranges import environment, resolve_range
from traceloom.web.templating import render

router = APIRouter(tags=["dashboard"])

LEVEL_ORDER = ("DEBUG", "DEFAULT", "WARNING", "ERROR")
LEVEL_COLOR = {
    "DEBUG": "--line-strong",
    "DEFAULT": "--ink-3",
    "WARNING": "--warn-bar",
    "ERROR": "--err-bar",
}
RANGE_LABEL = {"1h": "hour", "24h": "24 hours", "7d": "7 days", "30d": "30 days"}
SERIES = 6


def compact(value: float) -> str:
    """1,234 -> 1.2k; 1,500,000 -> 1.50M."""
    value = float(value)
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs(value) >= 10_000:
        return f"{value / 1000:.1f}k"
    if abs(value) >= 1000:
        return f"{value / 1000:.2f}k"
    return f"{value:,.0f}"


def _bars(rows: list[dict], key: str) -> list[dict]:
    """Horizontal-bar rows: each row's width relative to the largest value."""
    top = max((r[key] for r in rows), default=0) or 1
    return [
        {"label": r["model"], "value": r[key], "pct": r[key] / top * 100, "series": i % SERIES + 1}
        for i, r in enumerate(rows)
    ]


def _latency(rows: list[dict], label_key: str) -> list[dict]:
    top = max((r["p99"] for r in rows), default=0) or 1
    return [
        {"label": r[label_key] or "(unnamed)", **{k: r[k] for k in ("p50", "p90", "p95", "p99")},
         "px": r["p99"] / top * 60, "count": r.get("count")}
        for r in rows
    ]  # fmt: skip


def build_view(data: DashboardData) -> dict:
    levels = [dict(r, color=LEVEL_COLOR.get(r["level"], "--ink-3")) for r in data.level_share]
    levels.sort(key=lambda r: LEVEL_ORDER.index(r["level"]) if r["level"] in LEVEL_ORDER else 9)
    errors = next((r for r in levels if r["level"] == "ERROR"), None)
    tokens = sum(r["tokens"] for r in data.tokens_by_model)
    buckets = data.traces_over_time
    return {
        "trace_count": data.trace_count,
        "total_cost": data.total_cost,
        "total_tokens": tokens,
        "total_tokens_label": compact(tokens),
        "error_share": errors["share"] if errors else 0.0,
        "error_count": errors["count"] if errors else 0,
        "levels": levels,
        "cost_rows": _bars(data.cost_by_model, "cost"),
        "token_rows": _bars(data.tokens_by_model, "tokens"),
        "latency_names": _latency(data.latency_by_trace_name, "name"),
        "latency_models": _latency(data.latency_by_model, "model"),
        "top_users": data.top_users_by_cost,
        "series_rows": [{"bucket": b["bucket"], "count": b["count"]} for b in buckets],
        "chart": {
            "kind": "bar",
            "labels": [b["bucket"].isoformat() for b in buckets],
            "series": [{"name": "Traces", "values": [b["count"] for b in buckets]}],
        },
    }


@router.get("/project/{project_id}/dashboard", response_class=HTMLResponse)
def dashboard_page(
    request: Request,
    access: Access = Depends(require(Perm.TRACES_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    params = dict(request.query_params)
    range_key, start, end = resolve_range(params)
    env = environment(params)
    bucket = "hour" if range_key in ("1h", "24h") else "day"
    data = container.trace_store.dashboard(
        access.project.id,
        DashboardQuery(from_time=start, to_time=end, environment=env, bucket=bucket),
    )
    view = build_view(data)
    context = page_context(
        db,
        access,
        active="dashboard",
        v=view,
        range_key=range_key,
        range_label=RANGE_LABEL[range_key],
        keep={k: v for k, v in params.items() if k != "range"},
        environment=env,
        bucket=bucket,
        empty=data.trace_count == 0,
        base=f"/project/{access.project.id}",
    )
    return render(request, "dashboard.html", context)
