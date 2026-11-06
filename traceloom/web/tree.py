"""Pure helpers behind the trace detail views: tree, timeline and agent graph.

Everything takes `ObservationRow`s and returns plain data that templates draw. No I/O.
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from traceloom.ports import ObservationRow


def duration_ms(obs: ObservationRow) -> float:
    if obs.end_time is None:
        return 0.0
    return max((obs.end_time - obs.start_time).total_seconds() * 1000.0, 0.0)


def _order(obs: ObservationRow) -> tuple[datetime, str]:
    return (obs.start_time, obs.id)


# --- Tree -----------------------------------------------------------------------------------


@dataclass
class TreeNode:
    obs: ObservationRow
    depth: int = 0
    children: list[TreeNode] = field(default_factory=list)


def build_tree(observations: list[ObservationRow]) -> list[TreeNode]:
    """Nest observations by parent. Roots are those with no parent or an unknown parent
    (orphans). Siblings are in start order. A parent cycle never loops: its members are
    promoted to roots."""
    nodes = {o.id: TreeNode(o) for o in sorted(observations, key=_order)}
    roots: list[TreeNode] = []
    for node in nodes.values():
        parent = nodes.get(node.obs.parent_observation_id or "")
        if parent is None or parent is node:
            roots.append(node)
        else:
            parent.children.append(node)

    reached: set[str] = set()

    def walk(node: TreeNode, depth: int) -> None:
        node.depth = depth
        reached.add(node.obs.id)
        for child in node.children:
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    for node in nodes.values():  # members of a cycle are unreachable from any root
        if node.obs.id not in reached:
            parent = nodes[node.obs.parent_observation_id]
            parent.children.remove(node)
            roots.append(node)
            walk(node, 0)
    return sorted(roots, key=lambda n: _order(n.obs))


def flatten(roots: list[TreeNode]) -> list[TreeNode]:
    """Depth-first, pre-order rows for a tree or timeline listing."""
    out: list[TreeNode] = []

    def visit(node: TreeNode) -> None:
        out.append(node)
        for child in node.children:
            visit(child)

    for root in roots:
        visit(root)
    return out


def filter_debug(observations: list[ObservationRow], show_debug: bool) -> list[ObservationRow]:
    """Drop DEBUG-level observations unless asked. Children of a dropped observation move up
    to its nearest kept ancestor so the tree stays connected."""
    if show_debug:
        return list(observations)
    by_id = {o.id: o for o in observations}

    def kept_ancestor(parent_id: str | None) -> str | None:
        seen: set[str] = set()
        while parent_id and parent_id in by_id and parent_id not in seen:
            seen.add(parent_id)
            parent = by_id[parent_id]
            if parent.level != "DEBUG":
                return parent_id
            parent_id = parent.parent_observation_id
        return parent_id if parent_id not in by_id else None

    out = []
    for obs in observations:
        if obs.level == "DEBUG":
            continue
        new_parent = kept_ancestor(obs.parent_observation_id)
        out.append(
            obs
            if new_parent == obs.parent_observation_id
            else dataclasses.replace(obs, parent_observation_id=new_parent)
        )
    return out


def first_error(observations: list[ObservationRow]) -> ObservationRow | None:
    """The earliest ERROR observation (ties by id)."""
    errors = [o for o in observations if o.level == "ERROR"]
    return min(errors, key=_order) if errors else None


# --- Timeline -------------------------------------------------------------------------------


@dataclass(frozen=True)
class TimelineBar:
    id: str
    name: str
    type: str
    level: str
    depth: int
    start_ms: float  # from the start of the axis
    duration_ms: float
    left_pct: float
    width_pct: float
    ttft_pct: float | None  # marker position on the shared axis, generations with a first token
    ttft_ms: float | None


@dataclass(frozen=True)
class Timeline:
    total_ms: float
    bars: list[TimelineBar]
    ticks: list[tuple[float, float]]  # (pct, ms)


MIN_BAR_PCT = 0.4
TICK_COUNT = 4


def timeline(observations: list[ObservationRow]) -> Timeline:
    """Gantt geometry on one axis from the earliest start to the latest end, rows in tree order."""
    rows = flatten(build_tree(observations))
    if not rows:
        return Timeline(0.0, [], [])
    origin = min(n.obs.start_time for n in rows)
    end = max((n.obs.end_time or n.obs.start_time) for n in rows)
    end = max(end, *(n.obs.completion_start_time or origin for n in rows))
    total = max((end - origin).total_seconds() * 1000.0, 0.0)

    def pct(ms: float) -> float:
        return round(ms / total * 100.0, 4) if total else 0.0

    bars = []
    for node in rows:
        obs = node.obs
        start = (obs.start_time - origin).total_seconds() * 1000.0
        dur = duration_ms(obs)
        width = max(pct(dur), MIN_BAR_PCT)
        left = min(pct(start), 100.0 - width)
        ttft_ms = ttft_pct = None
        if obs.completion_start_time is not None:
            ttft_ms = max((obs.completion_start_time - obs.start_time).total_seconds() * 1000, 0.0)
            ttft_pct = pct(start + ttft_ms)
        bars.append(
            TimelineBar(
                id=obs.id,
                name=obs.name or obs.type,
                type=obs.type,
                level=obs.level,
                depth=node.depth,
                start_ms=start,
                duration_ms=dur,
                left_pct=left,
                width_pct=width,
                ttft_pct=ttft_pct,
                ttft_ms=ttft_ms,
            )
        )
    ticks = [(i / TICK_COUNT * 100.0, i / TICK_COUNT * total) for i in range(TICK_COUNT + 1)]
    return Timeline(total, bars, ticks)


# --- Agent graph ----------------------------------------------------------------------------

NODE_W, NODE_H = 160, 48
GAP_X, GAP_Y = 64, 28
PAD = 16


@dataclass(frozen=True)
class GraphNode:
    key: str  # "<type>:<name>"
    type: str
    name: str
    count: int
    total_ms: float
    first_observation_id: str
    has_error: bool
    layer: int
    x: int
    y: int
    w: int = NODE_W
    h: int = NODE_H


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    kind: str  # call (parent -> child) | next (sibling sequence)
    count: int
    x1: int
    y1: int
    x2: int
    y2: int


@dataclass(frozen=True)
class AgentGraph:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    width: int
    height: int


def _step_key(obs: ObservationRow) -> str:
    return f"{obs.type}:{obs.name or ''}"


def agent_graph(observations: list[ObservationRow]) -> AgentGraph:
    """Distinct steps (type + name) as nodes with call count and total duration; edges for
    parent->child calls and for consecutive siblings. Layered left to right by the shallowest
    depth a step occurs at, top to bottom by first appearance. Self-edges are omitted (the
    count already shows repetition). Output is deterministic for the same input."""
    roots = build_tree(observations)
    rows = flatten(roots)
    if not rows:
        return AgentGraph([], [], 0, 0)

    stats: dict[str, dict] = {}
    for node in rows:  # pre-order, so the first hit is the earliest within its subtree
        obs = node.obs
        s = stats.setdefault(
            _step_key(obs),
            {"obs": obs, "count": 0, "ms": 0.0, "err": False, "layer": node.depth, "first": obs},
        )
        s["count"] += 1
        s["ms"] += duration_ms(obs)
        s["err"] = s["err"] or obs.level == "ERROR"
        s["layer"] = min(s["layer"], node.depth)
        if _order(obs) < _order(s["first"]):
            s["first"] = obs

    edge_counts: dict[tuple[str, str, str], int] = defaultdict(int)

    def add_edge(a: str, b: str, kind: str) -> None:
        if a != b:
            edge_counts[(a, b, kind)] += 1

    def visit(siblings: list[TreeNode], parent: TreeNode | None) -> None:
        for prev, nxt in zip(siblings, siblings[1:], strict=False):
            add_edge(_step_key(prev.obs), _step_key(nxt.obs), "next")
        for node in siblings:
            if parent is not None:
                add_edge(_step_key(parent.obs), _step_key(node.obs), "call")
            visit(node.children, node)

    visit(roots, None)

    by_layer: dict[int, list[str]] = defaultdict(list)
    for key, s in sorted(stats.items(), key=lambda kv: (_order(kv[1]["first"]), kv[0])):
        by_layer[s["layer"]].append(key)
    layers = sorted(by_layer)
    layer_index = {layer: i for i, layer in enumerate(layers)}

    nodes: dict[str, GraphNode] = {}
    for layer in layers:
        for row, key in enumerate(by_layer[layer]):
            s = stats[key]
            obs = s["first"]
            nodes[key] = GraphNode(
                key=key,
                type=obs.type,
                name=obs.name or "",
                count=s["count"],
                total_ms=round(s["ms"], 3),
                first_observation_id=obs.id,
                has_error=s["err"],
                layer=layer,
                x=PAD + layer_index[layer] * (NODE_W + GAP_X),
                y=PAD + row * (NODE_H + GAP_Y),
            )

    edges = []
    for (a, b, kind), count in sorted(edge_counts.items()):
        src, dst = nodes[a], nodes[b]
        if src.x < dst.x:  # left to right: right edge to left edge
            x1, y1, x2, y2 = src.x + src.w, src.y + src.h // 2, dst.x, dst.y + dst.h // 2
        elif src.x > dst.x:  # back edge: left edge to right edge
            x1, y1, x2, y2 = src.x, src.y + src.h // 2, dst.x + dst.w, dst.y + dst.h // 2
        elif src.y <= dst.y:  # same column: bottom to top
            x1, y1, x2, y2 = src.x + src.w // 2, src.y + src.h, dst.x + dst.w // 2, dst.y
        else:
            x1, y1, x2, y2 = src.x + src.w // 2, src.y, dst.x + dst.w // 2, dst.y + dst.h
        edges.append(GraphEdge(a, b, kind, count, x1, y1, x2, y2))

    width = PAD * 2 + len(layers) * NODE_W + (len(layers) - 1) * GAP_X
    tallest = max(len(keys) for keys in by_layer.values())
    height = PAD * 2 + tallest * NODE_H + (tallest - 1) * GAP_Y
    ordered = sorted(nodes.values(), key=lambda n: (n.layer, n.y, n.key))
    return AgentGraph(ordered, edges, width, height)
