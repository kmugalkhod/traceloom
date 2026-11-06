"""View-models for the trace detail page: tree rows, timeline, graph and the IO pane."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlencode

from traceloom.ports import ObservationRow, TraceDetail, TraceRow
from traceloom.web import tree
from traceloom.web.formatting import count, latency, money
from traceloom.web.render import MODES, Rendered, render_value

VIEWS = ("tree", "timeline", "graph")
AGENT_STEP_TYPES = {"agent", "tool", "chain"}


@dataclass
class DetailState:
    """What the URL says: selected observation, IO mode, left-pane view and the DEBUG toggle."""

    project_id: str
    trace_id: str
    obs: str | None = None
    mode: str = "formatted"
    view: str = "tree"
    debug: bool = False

    def page_url(self, **changes) -> str:
        values = {"obs": self.obs, "mode": self.mode, "view": self.view, "debug": int(self.debug)}
        values.update(changes)
        query = {
            k: v
            for k, v in values.items()
            if v not in (None, "", 0)
            and not (k == "mode" and v == "formatted")
            and not (k == "view" and v == "tree")
        }
        base = f"/project/{self.project_id}/traces/{self.trace_id}"
        return f"{base}?{urlencode(query)}" if query else base

    def playground_url(self, obs_id: str) -> str:
        """The playground pre-filled from a generation of this trace (Open in playground)."""
        query = urlencode({"trace": self.trace_id, "obs": obs_id})
        return f"/project/{self.project_id}/playground?{query}"

    def prompt_url(self, name: str, version: int | None) -> str:
        suffix = f"?version={version}" if version else ""
        return f"/project/{self.project_id}/prompts/{quote(name, safe='/')}{suffix}"

    def io_url(self, **changes) -> str:
        values = {"obs": self.obs, "mode": self.mode, "view": self.view, "debug": int(self.debug)}
        values.update(changes)
        query = {k: v for k, v in values.items() if v}
        return f"/project/{self.project_id}/traces/{self.trace_id}/io?{urlencode(query)}"


def parse_state(project_id: str, trace_id: str, params) -> DetailState:
    mode = params.get("mode")
    view = params.get("view")
    return DetailState(
        project_id=project_id,
        trace_id=trace_id,
        obs=params.get("obs") or None,
        mode=mode if mode in MODES else "formatted",
        view=view if view in VIEWS else "tree",
        debug=params.get("debug") == "1",
    )


def obs_cost(obs: ObservationRow) -> float | None:
    total = (obs.cost_details or {}).get("total")
    return float(total) if total is not None else None


@dataclass
class NodeRow:
    id: str
    name: str
    type: str
    level: str
    depth: int
    has_children: bool
    duration: str
    cost: str | None
    page_url: str
    io_url: str
    selected: bool
    playground_url: str | None = None  # generations only


def node_rows(roots: list[tree.TreeNode], state: DetailState) -> list[NodeRow]:
    rows = []
    for node in tree.flatten(roots):
        o = node.obs
        cost = obs_cost(o)
        rows.append(
            NodeRow(
                id=o.id,
                name=o.name or o.type,
                type=o.type,
                level=o.level,
                depth=node.depth,
                has_children=bool(node.children),
                duration=latency(tree.duration_ms(o)) if o.end_time else "–",
                cost=money(cost) if cost else None,
                page_url=state.page_url(obs=o.id),
                io_url=state.io_url(obs=o.id),
                selected=state.obs == o.id,
                playground_url=state.playground_url(o.id) if o.type == "generation" else None,
            )
        )
    return rows


@dataclass
class TimelineRow:
    bar: tree.TimelineBar
    duration: str
    ttft: str | None
    page_url: str
    io_url: str
    selected: bool
    indent: int


def timeline_rows(tl: tree.Timeline, state: DetailState) -> list[TimelineRow]:
    return [
        TimelineRow(
            bar=b,
            duration=latency(b.duration_ms),
            ttft=latency(b.ttft_ms) if b.ttft_ms is not None else None,
            page_url=state.page_url(obs=b.id),
            io_url=state.io_url(obs=b.id),
            selected=state.obs == b.id,
            indent=b.depth,
        )
        for b in tl.bars
    ]


def has_agent_steps(observations: list[ObservationRow]) -> bool:
    return any(o.type in AGENT_STEP_TYPES for o in observations)


def edge_path(e: tree.GraphEdge) -> str:
    """An elbow connector between two node edges."""
    if e.x1 == e.x2:
        return f"M{e.x1} {e.y1}V{e.y2}"
    mid = (e.x1 + e.x2) // 2
    return f"M{e.x1} {e.y1}H{mid}V{e.y2}H{e.x2}"


@dataclass
class IoPane:
    title: str
    kind: str  # trace | observation
    obs: ObservationRow | None
    trace: TraceRow
    type: str
    level: str
    id: str
    parent_name: str | None
    started: str | None
    duration: str | None
    ttft: str | None
    usage: list[tuple[str, str]]
    cost_total: str | None
    cost_source: str
    status_message: str | None
    params: list[tuple[str, str]]
    sections: list[tuple[str, Rendered]]
    mode: str
    mode_urls: dict[str, tuple[str, str]]  # mode -> (page url, io url)
    trace_url: str
    playground_url: str | None = None  # a generation can be reopened in the playground
    prompt_label: str | None = None  # "support v2" when the generation is linked to a prompt
    prompt_url: str | None = None


def io_pane(detail: TraceDetail, state: DetailState) -> IoPane:
    trace = detail.trace
    by_id = {o.id: o for o in detail.observations}
    obs = by_id.get(state.obs) if state.obs else None
    source = obs or trace
    started = ttft = duration = parent_name = None
    usage: list[tuple[str, str]] = []
    params: list[tuple[str, str]] = []
    cost_total = status = None
    cost_source = "none"
    if obs is not None:
        origin = min((o.start_time for o in detail.observations), default=obs.start_time)
        started = "+" + latency((obs.start_time - origin).total_seconds() * 1000)
        duration = latency(tree.duration_ms(obs)) if obs.end_time else None
        if obs.completion_start_time:
            ttft = latency(
                max((obs.completion_start_time - obs.start_time).total_seconds() * 1000, 0)
            )
        parent = by_id.get(obs.parent_observation_id or "")
        parent_name = (parent.name or parent.type) if parent else None
        usage = [(k.replace("_", " "), count(v)) for k, v in (obs.usage_details or {}).items()]
        params = [(k, str(v)) for k, v in (obs.model_parameters or {}).items()]
        cost = obs_cost(obs)
        cost_total = money(cost, 6) if cost is not None else None
        cost_source = obs.cost_source
        status = obs.status_message
    return IoPane(
        title=(obs.name or obs.type) if obs else (trace.name or trace.id),
        kind="observation" if obs else "trace",
        obs=obs,
        trace=trace,
        type=obs.type if obs else "trace",
        level=obs.level if obs else trace.max_level,
        id=source.id,
        parent_name=parent_name,
        started=started,
        duration=duration,
        ttft=ttft,
        usage=usage,
        cost_total=cost_total,
        cost_source=cost_source,
        status_message=status,
        params=params,
        sections=[
            ("Input", render_value(source.input, state.mode)),
            ("Output", render_value(source.output, state.mode)),
            ("Metadata", render_value(source.metadata or None, state.mode)),
        ],
        mode=state.mode,
        mode_urls={m: (state.page_url(mode=m), state.io_url(mode=m)) for m in MODES},
        trace_url=state.page_url(obs=None),
        playground_url=(
            state.playground_url(obs.id) if obs is not None and obs.type == "generation" else None
        ),
        prompt_label=_prompt_label(obs),
        prompt_url=(
            state.prompt_url(obs.prompt_name, obs.prompt_version)
            if obs is not None and obs.prompt_name
            else None
        ),
    )


def _prompt_label(obs: ObservationRow | None) -> str | None:
    if obs is None or not obs.prompt_name:
        return None
    return f"{obs.prompt_name} v{obs.prompt_version}" if obs.prompt_version else obs.prompt_name
