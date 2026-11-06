from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceloom.ports import ObservationRow
from traceloom.web.tree import (
    agent_graph,
    build_tree,
    filter_debug,
    first_error,
    flatten,
    timeline,
)

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def obs(id, parent=None, start=0, dur=100, type="span", name=None, level="DEFAULT", ttft=None):
    return ObservationRow(
        id=id,
        trace_id="t",
        parent_observation_id=parent,
        start_time=T0 + timedelta(milliseconds=start),
        end_time=None if dur is None else T0 + timedelta(milliseconds=start + dur),
        completion_start_time=None if ttft is None else T0 + timedelta(milliseconds=start + ttft),
        type=type,
        name=name or id,
        level=level,
    )


def test_tree_nests_by_parent_in_start_order():
    rows = [
        obs("c2", "root", 20),
        obs("root", None, 0, 500),
        obs("c1", "root", 10),
        obs("g", "c1", 11),
    ]
    roots = build_tree(rows)
    assert [r.obs.id for r in roots] == ["root"]
    assert [c.obs.id for c in roots[0].children] == ["c1", "c2"]
    assert [(n.obs.id, n.depth) for n in flatten(roots)] == [
        ("root", 0), ("c1", 1), ("g", 2), ("c2", 1)
    ]  # fmt: skip


def test_orphans_become_roots_and_cycles_do_not_loop():
    roots = build_tree([obs("a", "missing", 5), obs("b", None, 0)])
    assert [r.obs.id for r in roots] == ["b", "a"]
    cyc = build_tree([obs("x", "y", 0), obs("y", "x", 1), obs("self", "self", 2)])
    assert sorted(n.obs.id for n in flatten(cyc)) == ["self", "x", "y"]
    assert build_tree([]) == []


def test_first_error_is_the_earliest_error():
    rows = [
        obs("late", start=50, level="ERROR"),
        obs("early", start=10, level="ERROR"),
        obs("warn", start=0, level="WARNING"),
    ]
    assert first_error(rows).id == "early"
    assert first_error([obs("ok")]) is None
    assert first_error([]) is None


def test_debug_filter_reparents_children_to_nearest_kept_ancestor():
    rows = [
        obs("root", None, 0),
        obs("dbg", "root", 1, level="DEBUG"),
        obs("kid", "dbg", 2),
        obs("deep", "kid", 3, level="DEBUG"),
        obs("leaf", "deep", 4),
    ]
    shown = filter_debug(rows, show_debug=False)
    assert {o.id: o.parent_observation_id for o in shown} == {
        "root": None, "kid": "root", "leaf": "kid"
    }  # fmt: skip
    assert len(filter_debug(rows, show_debug=True)) == 5
    assert rows[2].parent_observation_id == "dbg"  # input is not mutated


def test_timeline_geometry_on_a_shared_axis_with_ttft_marker():
    rows = [
        obs("root", None, 0, 1000),
        obs("gen", "root", 200, 400, type="generation", ttft=100),
        obs("tool", "root", 600, 400),
    ]
    tl = timeline(rows)
    assert tl.total_ms == 1000
    root, gen, tool = tl.bars
    assert (root.left_pct, root.width_pct, root.depth) == (0, 100, 0)
    assert (gen.left_pct, gen.width_pct, gen.depth) == (20, 40, 1)
    assert (gen.ttft_pct, gen.ttft_ms) == (30, 100)
    assert tool.ttft_pct is None and tool.left_pct == 60
    assert tl.ticks[0] == (0, 0) and tl.ticks[-1] == (100, 1000)


def test_timeline_handles_empty_and_zero_length():
    assert timeline([]).bars == []
    tl = timeline([obs("instant", dur=None)])
    assert tl.total_ms == 0
    assert tl.bars[0].left_pct == 0 and 0 < tl.bars[0].width_pct <= 100


def agent_trace():
    return [
        obs("a", None, 0, 1000, type="agent", name="planner"),
        obs("t1", "a", 10, 100, type="tool", name="search"),
        obs("g1", "a", 120, 200, type="generation", name="llm"),
        obs("t2", "a", 330, 50, type="tool", name="search", level="ERROR"),
        obs("g2", "t1", 20, 30, type="generation", name="llm"),
    ]


def test_graph_nodes_have_counts_durations_and_layers():
    graph = agent_graph(agent_trace())
    nodes = {n.key: n for n in graph.nodes}
    assert set(nodes) == {"agent:planner", "tool:search", "generation:llm"}
    assert nodes["tool:search"].count == 2 and nodes["tool:search"].total_ms == 150
    assert nodes["tool:search"].has_error and not nodes["agent:planner"].has_error
    assert nodes["generation:llm"].count == 2 and nodes["generation:llm"].total_ms == 230
    assert nodes["tool:search"].first_observation_id == "t1"
    assert nodes["generation:llm"].layer == 1  # shallowest occurrence
    assert nodes["agent:planner"].layer == 0
    assert nodes["agent:planner"].x < nodes["tool:search"].x
    assert nodes["tool:search"].y != nodes["generation:llm"].y  # same layer, own row


def test_graph_edges_are_calls_and_sibling_sequence():
    graph = agent_graph(agent_trace())
    edges = {(e.source, e.target, e.kind): e.count for e in graph.edges}
    assert edges[("agent:planner", "tool:search", "call")] == 2
    assert edges[("agent:planner", "generation:llm", "call")] == 1
    assert edges[("tool:search", "generation:llm", "call")] == 1
    assert edges[("tool:search", "generation:llm", "next")] == 1  # t1 -> g1
    assert edges[("generation:llm", "tool:search", "next")] == 1  # g1 -> t2
    assert all(e.source != e.target for e in graph.edges)  # t1 -> t2 style repeats omitted


def test_graph_is_deterministic_and_handles_empty():
    rows = agent_trace()
    assert agent_graph(rows) == agent_graph(list(reversed(rows)))
    empty = agent_graph([])
    assert (empty.nodes, empty.edges, empty.width, empty.height) == ([], [], 0, 0)
    graph = agent_graph(rows)
    assert graph.width > 0 and graph.height > 0
    for e in graph.edges:
        assert 0 <= e.x1 <= graph.width and 0 <= e.y2 <= graph.height
