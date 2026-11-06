from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html import unescape

import pytest

from traceloom.auth.permissions import Role
from traceloom.ports import ObservationRow, TraceRow

H = {"Accept": "text/html"}
T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def ob(id_, name, type_="span", start=0.0, end=1.0, parent=None, **kw):
    return ObservationRow(
        id=id_,
        trace_id="tr1",
        name=name,
        type=type_,
        start_time=at(start),
        end_time=at(end),
        parent_observation_id=parent,
        event_ts=at(0),
        **kw,
    )


def seed_trace(container, project_id, trace_id="tr1", observations=None, **fields):
    trace = TraceRow(
        id=trace_id,
        timestamp=at(0),
        event_ts=at(0),
        name=fields.pop("name", "support-agent"),
        **fields,
    )
    obs = observations if observations is not None else agent_run()
    container.trace_store.upsert(project_id, [trace], obs, [])
    container.trace_store.refresh_trace_aggregates(project_id, [trace_id])


def agent_run():
    return [
        ob("root", "support-agent", "agent", 0, 6.8),
        ob(
            "plan",
            "planner",
            "generation",
            0.1,
            1.5,
            parent="root",
            model="claude-sonnet-4-5",
            completion_start_time=at(0.4),
            usage_details={"input": 900, "output": 120, "total": 1020},
            cost_details={"total": 0.0069},
            cost_source="calculated",
            model_parameters={"temperature": 0.2},
            input=[{"role": "user", "content": "Where is my order?"}],
            output="Let me **check**.",
        ),
        ob("kb", "search_kb", "tool", 1.5, 2.1, parent="root"),
        ob("emb", "embed-query", "embedding", 1.5, 1.6, parent="kb"),
        ob(
            "vec",
            "kb-vector-search",
            "retriever",
            1.6,
            2.0,
            parent="kb",
            level="WARNING",
            status_message="slow index",
        ),
        ob(
            "lookup",
            "lookup_order",
            "tool",
            2.2,
            3.1,
            parent="root",
            level="ERROR",
            status_message="order service timed out",
            input={"order_id": "ord_1"},
        ),
        ob("late-err", "retry-lookup", "tool", 3.2, 3.5, parent="root", level="ERROR"),
        ob("dbg", "debug-trace", "span", 3.6, 3.7, parent="root", level="DEBUG"),
        ob(
            "final",
            "final-answer",
            "generation",
            4.0,
            6.5,
            parent="root",
            model="gpt-4o",
            cost_details={"total": 0.01},
            cost_source="provided",
            usage_details={"input": 10, "output": 5},
        ),
    ]


def url(world, query="", trace_id="tr1"):
    return f"/project/{world.project.id}/traces/{trace_id}" + (f"?{query}" if query else "")


def io_url(world, query="", trace_id="tr1"):
    return f"/project/{world.project.id}/traces/{trace_id}/io" + (f"?{query}" if query else "")


@pytest.fixture
def run(container, world):
    seed_trace(
        container,
        world.project.id,
        user_id="usr_maria",
        session_id="sess-1",
        tags=["support", "refund"],
        release="2026.11.3",
        version="7",
    )
    return world.project.id


def test_header_shows_trace_facts(client, world, login, run):
    login(client, world.user(Role.VIEWER))
    html = client.get(url(world), headers=H).text
    assert "<h1>support-agent</h1>" in html and 'class="lv ERROR"' in html
    assert 'data-copy-from="#trace-id"' in html and ">tr1<" in html
    assert (
        f'/project/{world.project.id}/users/usr_maria"' in html
        and f'/project/{world.project.id}/sessions/sess-1"' in html
    )
    for label in ("Latency", "Cost", "Tokens", "Env", "Release", "Version"):
        assert f"<dt>{label}</dt>" in html
    assert ">support<" in html and ">refund<" in html and "2026.11.3" in html
    assert 'aria-current="page"' in html and 'class="sidenav"' in html


def test_tree_nests_three_levels_with_icons_durations_costs_and_level_colours(
    client, world, login, run
):
    login(client, world.user(Role.VIEWER))
    html = client.get(url(world), headers=H).text
    tree = html[html.index('role="tree"') : html.index("</ul>", html.index('role="tree"'))]
    levels = [int(x) for x in re.findall(r'aria-level="(\d)"', tree)]
    assert max(levels) >= 3 and levels[0] == 1
    assert re.search(r'class="ty ty-agent"', tree) and re.search(r'class="ty ty-generation"', tree)
    assert re.search(r'class="ty ty-retriever"', tree) and re.search(
        r'class="ty ty-embedding"', tree
    )
    assert "1.40 s" in tree and "$0.0069" in tree  # duration and cost of the planner
    assert re.search(r'class="node ERROR"[^>]*>.*?lookup_order', tree, flags=re.S)
    assert re.search(r'class="node WARNING"[^>]*>.*?kb-vector-search', tree, flags=re.S)
    assert "debug-trace" not in tree  # DEBUG is hidden by default


def test_debug_toggle_shows_debug_rows_and_keeps_children_attached(client, container, world, login):
    seed_trace(
        container,
        world.project.id,
        observations=[
            ob("root", "run", "agent", 0, 5),
            ob("dbg", "debug-parent", "span", 0.5, 4.5, parent="root", level="DEBUG"),
            ob("child", "kept-child", "tool", 1, 2, parent="dbg"),
        ],
    )
    login(client, world.user(Role.VIEWER))
    default = client.get(url(world), headers=H).text
    assert "debug-parent" not in default and "kept-child" in default
    assert re.search(
        r'aria-level="2"[^>]*>(?:(?!</li>).)*kept-child', default, flags=re.S
    )  # moved up under root
    assert "Show DEBUG (1)" in default
    shown = client.get(url(world, "debug=1"), headers=H).text
    assert "debug-parent" in shown and 'aria-checked="true"' in shown


def test_timeline_has_gantt_bars_and_a_ttft_marker_on_generations(client, world, login, run):
    login(client, world.user(Role.VIEWER))
    html = client.get(url(world, "view=timeline"), headers=H).text
    body = html[html.index('data-body="timeline"') : html.index('data-body="graph"')]
    assert 'data-body="tree" hidden' in html and 'data-body="timeline">' in html
    assert (
        len(re.findall(r'<rect class="b', body)) == 8
    )  # every visible observation; DEBUG is hidden
    rects = re.findall(r'<rect class="b[^"]*" x="([\d.]+)" y="8" width="([\d.]+)"', body)
    assert all(0 <= float(x) <= 100 and 0 < float(w) <= 100 for x, w in rects)
    assert (
        body.count('<line class="t"') == 1 and "Time to first token 300 ms" in body
    )  # only the planner has one
    assert re.search(r'<rect class="b ERROR"', body) and re.search(r'<rect class="b WARNING"', body)
    assert "0 ms" in body and "6.80 s" in body  # axis from 0 to the end of the run
    assert not re.search(r"\sstyle=", html)


def test_generation_pane_shows_model_usage_and_cost_with_its_source(client, world, login, run):
    login(client, world.user(Role.VIEWER))
    calc = client.get(io_url(world, "obs=plan"), headers=H).text
    assert (
        "claude-sonnet-4-5" in calc
        and "input 900" in calc
        and "output 120" in calc
        and "total 1,020" in calc
    )
    assert (
        "$0.006900" in calc
        and 'class="src calc">calculated<' in calc
        and "temperature 0.2" in calc
        and "300 ms" in calc
    )
    provided = client.get(io_url(world, "obs=final"), headers=H).text
    assert "gpt-4o" in provided and 'class="src provided">provided<' in provided
    spanless = client.get(io_url(world, "obs=kb"), headers=H).text
    assert "Generation details" not in spanless


def test_error_status_message_is_shown_and_the_jump_link_targets_the_first_error(
    client, world, login, run
):
    login(client, world.user(Role.VIEWER))
    html = client.get(url(world), headers=H).text
    jump = re.search(
        r'<a class="btn btn-sm" href="([^"]+)"[^>]*data-jump[^>]*data-obs="([^"]+)"', html
    )
    assert jump and jump.group(2) == "lookup"  # the earlier of the two ERROR observations
    assert unescape(jump.group(1)).endswith("?obs=lookup")
    assert "order service timed out" in client.get(unescape(jump.group(1)), headers=H).text


def test_no_jump_link_without_errors(client, container, world, login):
    seed_trace(container, world.project.id, observations=[ob("a", "fine", "span", 0, 1)])
    login(client, world.user(Role.VIEWER))
    assert "Jump to first error" not in client.get(url(world), headers=H).text


def test_graph_tab_only_for_traces_with_agent_tool_or_chain_steps(
    client, container, world, login, run
):
    login(client, world.user(Role.VIEWER))
    with_graph = client.get(url(world, "view=graph"), headers=H).text
    assert (
        'data-view="graph"' in with_graph
        and 'data-body="graph">' in with_graph
        and "<svg viewBox" in with_graph
    )
    seed_trace(
        container,
        world.project.id,
        "plain",
        observations=[
            ObservationRow(
                id="p1",
                trace_id="plain",
                name="a",
                type="span",
                start_time=at(0),
                end_time=at(1),
                event_ts=at(0),
            ),
            ObservationRow(
                id="p2",
                trace_id="plain",
                name="b",
                type="generation",
                start_time=at(1),
                end_time=at(2),
                event_ts=at(0),
            ),
        ],
    )
    plain = client.get(url(world, "view=graph", "plain"), headers=H).text
    assert 'data-view="graph"' not in plain and 'data-body="graph"' not in plain
    assert 'data-body="tree">' in plain  # asking for the graph falls back to the tree


def test_graph_nodes_select_the_first_observation_of_their_step(client, world, login, run):
    login(client, world.user(Role.VIEWER))
    html = client.get(url(world, "view=graph"), headers=H).text
    graph = html[html.index('data-body="graph"') :]
    anchors = re.findall(
        r'<a href="([^"]+)" hx-get="([^"]+)"[^>]*data-obs="([^"]+)">\s*'
        r'<g class="gn([^"]*)"[^>]*aria-label="([^"]+)"',
        graph,
    )
    by_label = {
        label.split(",")[0]: (page, partial, obs, cls) for page, partial, obs, cls, label in anchors
    }
    assert set(by_label) >= {
        "support-agent",
        "planner",
        "search_kb",
        "lookup_order",
        "final-answer",
    }
    page, partial, obs_id, cls = by_label["lookup_order"]
    assert obs_id == "lookup" and unescape(page).endswith("obs=lookup&view=graph")
    assert (
        unescape(partial).startswith(f"/project/{world.project.id}/traces/tr1/io?")
        and "obs=lookup" in partial
    )
    assert "err" in cls  # the failing step is flagged
    assert "err" not in by_label["planner"][3]
    assert all("1 call" in label or "calls" in label for *_, label in anchors)


def test_selection_defaults_to_the_trace_and_stale_ids_fall_back(
    client, container, world, login, run
):
    login(client, world.user(Role.VIEWER))
    trace_pane = client.get(url(world), headers=H).text
    assert "<h2>support-agent</h2>" in trace_pane.split('id="io-pane"')[1]
    stale = client.get(url(world, "obs=does-not-exist"), headers=H)
    assert (
        stale.status_code == 200 and "<h2>support-agent</h2>" in stale.text.split('id="io-pane"')[1]
    )
    assert client.get(io_url(world, "obs=does-not-exist"), headers=H).status_code == 404
    selected = client.get(url(world, "obs=lookup"), headers=H).text
    assert "<h2>lookup_order</h2>" in selected.split('id="io-pane"')[1]
    assert re.search(r'aria-selected="true" aria-level="2" data-obs="lookup"', selected)


def test_io_modes_formatted_json_and_markdown(client, world, login, run):
    login(client, world.user(Role.VIEWER))
    formatted = client.get(io_url(world, "obs=plan"), headers=H).text
    assert (
        "<html" not in formatted
        and 'class="msg msg-user"' in formatted
        and "Where is my order?" in formatted
    )
    assert 'aria-selected="true" href="' in formatted
    as_json = client.get(io_url(world, "obs=plan&mode=json"), headers=H).text
    assert (
        "[{&#34;role&#34;: &#34;user&#34;, &#34;content&#34;: &#34;Where is my order?&#34;}]"
        in as_json
    )
    markdown = client.get(io_url(world, "obs=plan&mode=markdown"), headers=H).text
    assert "<strong>check</strong>" in markdown and 'class="md"' in markdown
    assert (
        client.get(io_url(world, "obs=plan&mode=bogus"), headers=H).status_code == 200
    )  # falls back to formatted


def test_mode_links_carry_the_selection_and_view(client, world, login, run):
    login(client, world.user(Role.VIEWER))
    html = client.get(url(world, "obs=plan&view=timeline&debug=1"), headers=H).text
    link = re.search(
        r'<a role="tab" aria-selected="false" href="([^"]+)" hx-get="([^"]+)"'
        r' hx-target="#io-pane"[^>]*hx-push-url="([^"]+)">JSON',
        html,
    )
    page, partial = unescape(link.group(1)), unescape(link.group(2))
    assert (
        "mode=json" in page and "obs=plan" in page and "view=timeline" in page and "debug=1" in page
    )
    assert partial.startswith(f"/project/{run}/traces/tr1/io?") and "view=timeline" in partial


def test_viewer_can_read_everything_outsiders_get_404(
    client, world, make_user, login, run, make_project, container
):
    login(client, world.user(Role.VIEWER))
    assert client.get(url(world), headers=H).status_code == 200
    assert client.get(url(world, trace_id="nope"), headers=H).status_code == 404
    other = make_project(world.org, "other")
    assert (
        client.get(f"/project/{other.id}/traces/tr1", headers=H).status_code == 404
    )  # another project's id space
    login(client, make_user())
    assert client.get(url(world), headers=H).status_code == 404
    assert client.get(io_url(world, "obs=plan"), headers=H).status_code == 404


def test_missing_trace_is_the_404_page_inside_the_shell(client, world, login, run):
    login(client, world.user(Role.VIEWER))
    r = client.get(url(world, trace_id="ghost"), headers=H)
    assert (
        r.status_code == 404 and "Page not found" in r.text and "There is no trace ghost" in r.text
    )


def test_trace_with_no_observations_renders(client, container, world, login):
    seed_trace(container, world.project.id, observations=[])
    login(client, world.user(Role.VIEWER))
    html = client.get(url(world), headers=H).text
    assert "This trace has no observations." in html and 'data-view="graph"' not in html


def test_page_has_no_inline_script_or_style_and_loads_its_own_js(client, world, login, run):
    login(client, world.user(Role.VIEWER))
    html = client.get(url(world), headers=H).text
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and not re.search(r"\sstyle=", html)
    assert 'src="/static/trace.js"' in html
    js = client.get("/static/trace.js").text
    for needle in ("e.key === 't'", "e.key === 'e'", "data-jump", "data-view"):
        assert needle in js


def test_page_slots_for_collaboration_partials_are_included_when_present(
    client, world, login, run, monkeypatch
):
    from jinja2 import DictLoader

    from traceloom.web.templating import templates

    real = templates.env.loader
    monkeypatch.setattr(
        templates.env,
        "loader",
        __import__("jinja2").ChoiceLoader(
            [
                DictLoader(
                    {
                        "traces/_comments.html": "<!--comments-slot-->",
                        "traces/_scores.html": "<!--scores-slot-->",
                        "traces/_actions.html": "<!--actions-slot-->",
                    }
                ),
                real,
            ]
        ),
    )
    login(client, world.user(Role.VIEWER))
    html = client.get(url(world), headers=H).text
    assert (
        "<!--comments-slot-->" in html
        and "<!--scores-slot-->" in html
        and "<!--actions-slot-->" in html
    )
