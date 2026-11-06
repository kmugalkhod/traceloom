"""ClickHouse adapter: query building, row shapes and the schema, against a fake client."""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from traceloom.adapters.full import clickhouse_trace_store as ch
from traceloom.adapters.full.clickhouse_trace_store import ClickHouseTraceStore
from traceloom.db.models import Observation, Score, Trace
from traceloom.ports import (
    DashboardQuery,
    InvalidCursor,
    ObservationRow,
    SessionQuery,
    TraceQuery,
    TraceRow,
    UserQuery,
    decode_cursor,
    encode_cursor,
)

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
PID = "proj-1"
FIRST_PREDICATE = "project_id = {project_id:String}"


class FakeClient:
    """Records every call; `results` maps a SQL fragment to (column_names, rows)."""

    def __init__(self, results=None):
        self.results = results or {}
        self.queries: list[tuple[str, dict]] = []
        self.inserts: list[tuple[str, list, list]] = []
        self.commands: list[tuple[str, dict | None]] = []

    def query(self, sql, parameters=None):
        self.queries.append((sql, parameters or {}))
        for fragment, (columns, rows) in self.results.items():
            if fragment in sql:
                return SimpleNamespace(column_names=columns, result_rows=rows)
        return SimpleNamespace(column_names=[], result_rows=[])

    def insert(self, table, data, column_names=None):
        self.inserts.append((table, data, column_names))

    def command(self, sql, parameters=None):
        self.commands.append((sql, parameters))


def where_of(sql: str) -> str:
    return sql.split(" WHERE ", 1)[1]


def assert_project_scoped(sql: str, params: dict) -> None:
    assert where_of(sql).startswith(FIRST_PREDICATE), sql
    assert params["project_id"] == PID


def trace_record(id="t1", **over):
    base = dict.fromkeys(ch.TRACE_COLUMNS)
    base.update(
        id=id, project_id=PID, name="n", timestamp=T0, tags=["a"], metadata='{"k": 1}',
        input='"hi"', output=None, environment="default", public=0, bookmarked=1, event_ts=T0,
        latency_ms=12.5, total_tokens=3, total_cost=0.5, max_level="ERROR", observation_count=2,
        search_text="n hi",
    )  # fmt: skip
    base.update(over)
    return base


def as_result(records, columns):
    return (columns, [[r[c] for c in columns] for r in records])


# --- Schema ----------------------------------------------------------------------------------


def schema_statements():
    return ch.split_statements(ch.SCHEMA_FILE.read_text())


def test_schema_has_three_replacing_tables_with_balanced_syntax():
    statements = schema_statements()
    assert len(statements) == 3
    for statement in statements:
        assert statement.startswith("CREATE TABLE IF NOT EXISTS ")
        assert statement.count("(") == statement.count(")")
        assert "ENGINE = ReplacingMergeTree(event_ts)" in statement
        assert re.search(r"ORDER BY \(project_id, ", statement)


def test_schema_columns_match_the_lite_models():
    by_table = {re.search(r"EXISTS (\w+)", s).group(1): s for s in schema_statements()}
    for name, model in (("traces", Trace), ("observations", Observation), ("scores", Score)):
        body = by_table[name].split("ENGINE")[0]
        declared = re.findall(r"^    (?!INDEX)(\w+) ", body, flags=re.M)
        assert declared == [c.name for c in model.__table__.columns], name


def test_schema_search_uses_ngram_bloom_filter_indexes():
    traces = schema_statements()[0]
    assert "INDEX idx_search lower(search_text) TYPE ngrambf_v1" in traces
    assert "INDEX idx_name" in traces and "INDEX idx_id" in traces


def test_apply_schema_runs_each_statement_in_the_database():
    client = FakeClient()
    ch.apply_schema(client, "tl_test")
    sql = [c[0] for c in client.commands]
    assert sql[0] == "CREATE DATABASE IF NOT EXISTS tl_test"
    assert [s.split("(")[0].strip() for s in sql[1:]] == [
        f"CREATE TABLE IF NOT EXISTS tl_test.{t}" for t in ("traces", "observations", "scores")
    ]
    with pytest.raises(ValueError):
        ch.apply_schema(client, "bad; DROP")
    with pytest.raises(ValueError):
        ClickHouseTraceStore(client, "a.b")


def test_default_run_never_imports_the_clickhouse_client():
    assert "clickhouse_connect" not in sys.modules


# --- List queries ----------------------------------------------------------------------------


def test_list_query_is_project_scoped_and_reads_final():
    sql, params = ch.build_list_traces(PID, TraceQuery())
    assert_project_scoped(sql, params)
    assert " FROM traces FINAL " in sql
    assert sql.endswith("ORDER BY timestamp DESC, id ASC LIMIT 51")


def test_every_filter_becomes_a_bound_parameter():
    q = TraceQuery(
        q="Refund_%", name="n", user_id="u", session_id="s", tags=["a", "b"], min_level="WARNING",
        environment="prod", release="r", version="v", has_error=True, metadata={"region": "eu"},
        latency_min_ms=1, latency_max_ms=2, cost_min=0.1, cost_max=0.2, bookmarked=True,
        from_time=T0, to_time=T0 + timedelta(days=1),
    )  # fmt: skip
    sql, params = ch.build_list_traces(PID, q)
    assert_project_scoped(sql, params)
    for fragment in (
        "lower(search_text) LIKE {q:String}",
        "lower(coalesce(name, '')) LIKE {q:String}",
        "lower(id) LIKE {q:String}",
        "hasAny(tags, {tags:Array(String)})",
        "max_level IN {levels:Array(String)}",
        "max_level = 'ERROR'",
        "JSONExtractString(metadata, {mk0:String})",
        "latency_ms >= {latency_min:Float64}",
        "latency_ms <= {latency_max:Float64}",
        "total_cost >= {cost_min:Float64}",
        "total_cost <= {cost_max:Float64}",
        "bookmarked = {bookmarked:Bool}",
        "timestamp >= {from_time:DateTime64(3, 'UTC')}",
        "timestamp <= {to_time:DateTime64(3, 'UTC')}",
    ):
        assert fragment in sql, fragment
    for column in ("name", "user_id", "session_id", "environment", "release", "version"):
        assert f"{column} = {{{column}:String}}" in sql
    assert params["q"] == "%refund\\_\\%%"  # lower-cased, wildcards escaped
    assert params["levels"] == ["WARNING", "ERROR"]
    assert (params["mk0"], params["mv0"]) == ("region", "eu")
    assert "Refund" not in sql and "region" not in sql  # values are parameters, not text


def test_user_values_are_never_interpolated_into_sql():
    evil = "x'; DROP TABLE traces; --"
    sql, params = ch.build_list_traces(
        PID, TraceQuery(q=evil, name=evil, tags=[evil], metadata={evil: evil})
    )
    assert "DROP" not in sql
    assert params["name"] == evil and params["tags"] == [evil]
    assert (params["mk0"], params["mv0"]) == (evil, evil)


def test_has_error_false_and_bad_input():
    sql, _ = ch.build_list_traces(PID, TraceQuery(has_error=False))
    assert "max_level != 'ERROR'" in sql
    with pytest.raises(ValueError):
        ch.build_list_traces(PID, TraceQuery(min_level="LOUD"))
    with pytest.raises(ValueError):
        ch.build_list_traces(PID, TraceQuery(sort="nope"))


@pytest.mark.parametrize(
    ("sort", "expr", "type_"),
    [
        ("timestamp", "timestamp", "DateTime64(3, 'UTC')"),
        ("latency", "coalesce(latency_ms, -1)", "Float64"),
        ("cost", "total_cost", "Float64"),
        ("tokens", "total_tokens", "Float64"),
    ],
)
@pytest.mark.parametrize("descending", [True, False])
def test_cursor_paging_is_keyset_on_sort_value_then_id(sort, expr, type_, descending):
    value = T0 if sort == "timestamp" else 2.5
    cursor = encode_cursor(value, "t9")
    sql, params = ch.build_list_traces(
        PID, TraceQuery(sort=sort, descending=descending, cursor=cursor, limit=10)
    )
    assert_project_scoped(sql, params)
    op = "<" if descending else ">"
    assert f"({expr} {op} {{cursor_value:{type_}}} OR ({expr} = {{cursor_value:{type_}}}" in sql
    assert "id > {cursor_id:String}" in sql
    assert params["cursor_value"] == value and params["cursor_id"] == "t9"
    assert sql.endswith(f"ORDER BY {expr} {'DESC' if descending else 'ASC'}, id ASC LIMIT 11")


def test_bad_or_mismatched_cursors_are_rejected():
    with pytest.raises(InvalidCursor):
        ch.build_list_traces(PID, TraceQuery(cursor="junk"))
    with pytest.raises(InvalidCursor):
        ch.build_list_traces(PID, TraceQuery(sort="cost", cursor=encode_cursor(T0, "x")))
    with pytest.raises(InvalidCursor):
        ch.build_list_traces(PID, TraceQuery(sort="timestamp", cursor=encode_cursor(1.0, "x")))


def test_limit_is_capped():
    sql, _ = ch.build_list_traces(PID, TraceQuery(limit=10_000))
    assert sql.endswith(f"LIMIT {ch.MAX_PAGE_SIZE + 1}")


def test_list_traces_maps_rows_attaches_scores_and_emits_next_cursor():
    records = [trace_record("t1", timestamp=T0), trace_record("t2", timestamp=T0 - timedelta(1))]
    score = dict.fromkeys(ch.SCORE_COLUMNS, None)
    score.update(id="s1", trace_id="t1", name="q", value=0.9, timestamp=T0, event_ts=T0)
    client = FakeClient(
        {
            "FROM db.traces": as_result(records, ch.TRACE_COLUMNS),
            "FROM db.scores": as_result([score], ch.SCORE_COLUMNS),
        }
    )
    page = ClickHouseTraceStore(client, "db").list_traces(PID, TraceQuery(limit=1))
    (item,) = page.items
    assert (item.id, item.tags, item.metadata, item.input) == ("t1", ["a"], {"k": 1}, "hi")
    assert item.bookmarked is True and item.public is False
    assert [s.name for s in item.scores] == ["q"]
    value, row_id = decode_cursor(page.next_cursor)
    assert (value, row_id) == (T0, "t1")
    scores_sql, scores_params = client.queries[1]
    assert_project_scoped(scores_sql, scores_params)
    assert scores_params["ids"] == ["t1"]


def test_last_page_has_no_cursor():
    client = FakeClient({"FROM db.traces": as_result([trace_record()], ch.TRACE_COLUMNS)})
    assert ClickHouseTraceStore(client, "db").list_traces(PID, TraceQuery()).next_cursor is None


def test_get_trace_builds_three_scoped_queries():
    obs = dict.fromkeys(ch.OBSERVATION_COLUMNS, None)
    obs.update(id="o1", trace_id="t1", start_time=T0, metadata="{}", usage_details='{"total": 3}',
               model_parameters="{}", cost_details="{}", type="span", level="DEFAULT",
               environment="default", cost_source="none", usage_source="none")  # fmt: skip
    client = FakeClient(
        {
            "FROM db.traces": as_result([trace_record()], ch.TRACE_COLUMNS),
            "FROM db.observations": as_result([obs], ch.OBSERVATION_COLUMNS),
        }
    )
    detail = ClickHouseTraceStore(client, "db").get_trace(PID, "t1")
    assert [o.id for o in detail.observations] == ["o1"]
    assert detail.observations[0].usage_details == {"total": 3}
    assert len(client.queries) == 3
    for sql, params in client.queries:
        assert_project_scoped(sql, params)
        assert " FINAL " in sql
    assert ClickHouseTraceStore(FakeClient(), "db").get_trace(PID, "missing") is None


# --- Sessions, users, dashboard --------------------------------------------------------------


def test_session_and_user_lists_are_scoped_grouped_and_keyset_paged():
    cursor = encode_cursor(T0, "s5")
    sql, params = ch.build_list_sessions(
        PID, SessionQuery(environment="prod", from_time=T0, sort="last_trace", cursor=cursor)
    )
    assert_project_scoped(sql, params)
    assert "GROUP BY session_id HAVING last_trace_time < {cursor_value:DateTime64(3, 'UTC')}" in sql
    assert "session_id > {cursor_id:String}" in sql
    assert "environment = {environment:String}" in sql
    assert "ORDER BY last_trace_time DESC, session_id ASC LIMIT 51" in sql
    sql, params = ch.build_list_users(PID, UserQuery(sort="cost", descending=False))
    assert_project_scoped(sql, params)
    assert "ORDER BY cost_sum ASC, user_id ASC" in sql and "user_id IS NOT NULL" in sql
    with pytest.raises(InvalidCursor):
        ch.build_list_users(PID, UserQuery(sort="cost", cursor=cursor))
    with pytest.raises(ValueError):
        ch.build_list_sessions(PID, SessionQuery(sort="bogus"))


def test_session_detail_and_user_summary_queries_are_scoped():
    builders = [
        ch.build_get_session(PID, "s1"),
        ch.build_session_traces(PID, "s1"),
        ch.build_get_user(PID, "u1"),
        ch.build_user_daily(PID, "u1"),
        ch.build_user_recent_traces(PID, "u1"),
        ch.build_user_recent_sessions(PID, "u1"),
        ch.build_user_recent_scores(PID, "u1", "traces", "scores"),
    ]
    for sql, params in builders:
        assert_project_scoped(sql, params)
    scores_sql = builders[-1][0]
    assert scores_sql.count("project_id = {project_id:String}") == 2  # subquery is scoped too


def test_session_row_shape():
    record = {
        "session_id": "s1", "first_trace_time": T0, "last_trace_time": T0 + timedelta(1),
        "trace_count": 2, "duration_ms": 5.0, "cost_sum": 1.5, "user_ids": ["b", "a"],
        "tag_list": ["z", "y"],
    }  # fmt: skip
    client = FakeClient({"GROUP BY session_id": as_result([record], list(record))})
    page = ClickHouseTraceStore(client, "db").list_sessions(PID, SessionQuery())
    row = page.items[0]
    assert (row.trace_count, row.total_cost, row.user_ids, row.tags) == (
        2,
        1.5,
        ["a", "b"],
        ["y", "z"],
    )


def test_dashboard_panels_are_scoped_and_use_percentile_and_buckets():
    query = DashboardQuery(
        from_time=T0, to_time=T0 + timedelta(days=1), environment="prod", bucket="day"
    )
    panels = ch.build_dashboard(PID, query, "db")
    assert set(panels) == {
        "totals", "cost_by_model", "tokens_by_model", "latency_by_trace_name",
        "latency_by_model", "level_share", "top_users_by_cost", "traces_over_time",
    }  # fmt: skip
    for sql, params in panels.values():
        assert_project_scoped(sql, params)
        assert " FINAL " in sql and "environment = {environment:String}" in sql
        assert params["from_time"] == T0
    assert "quantilesExact(0.5, 0.9, 0.95, 0.99)(latency_ms)" in panels["latency_by_trace_name"][0]
    assert "dateDiff('millisecond', start_time, end_time)" in panels["latency_by_model"][0]
    assert "INTERVAL 1 day" in panels["traces_over_time"][0]
    assert "LIMIT 10" in panels["top_users_by_cost"][0]
    assert "db.observations" in panels["level_share"][0]
    with pytest.raises(ValueError):
        ch.build_dashboard(PID, DashboardQuery(T0, T0, bucket="week"))


def test_dashboard_result_mapping():
    client = FakeClient(
        {
            "count() AS trace_count": (["trace_count", "cost_sum"], [[3, 1.25]]),
            "GROUP BY model ORDER BY cost": (["model", "cost"], [["gpt", 1.0]]),
            "GROUP BY model ORDER BY tokens": (["model", "tokens"], [["gpt", 40]]),
            "GROUP BY trace_name": (["trace_name", "q"], [["chat", [1, 2, 3, 4]]]),
            "GROUP BY level": (["level", "n"], [["DEFAULT", 3], ["ERROR", 1]]),
            "GROUP BY user_id": (["user_id", "cost"], [["u", 0.5]]),
            "GROUP BY bucket": (["bucket", "n"], [[T0, 3]]),
        }
    )
    data = ClickHouseTraceStore(client, "db").dashboard(PID, DashboardQuery(T0, T0 + timedelta(1)))
    assert (data.trace_count, data.total_cost) == (3, 1.25)
    assert data.latency_by_trace_name == [
        {"name": "chat", "p50": 1.0, "p90": 2.0, "p95": 3.0, "p99": 4.0}
    ]
    assert [(r["level"], r["share"]) for r in data.level_share] == [
        ("DEFAULT", 0.75),
        ("ERROR", 0.25),
    ]
    assert data.tokens_by_model == [{"model": "gpt", "tokens": 40}]
    assert len(client.queries) == 8


# --- Writes and row shape --------------------------------------------------------------------


def make_obs(id, start, dur, level="DEFAULT", name=None, tokens=0, cost=0.0):
    return ObservationRow(
        id=id, trace_id="t1", start_time=T0 + timedelta(seconds=start),
        end_time=T0 + timedelta(seconds=start + dur), level=level, name=name,
        usage_details={"total": tokens}, cost_details={"total": cost},
    )  # fmt: skip


def test_compute_aggregates_from_observations():
    trace = TraceRow(id="t1", timestamp=T0, name="chat", input={"q": "hello"}, output="bye")
    out = ch.compute_aggregates(
        trace,
        [make_obs("a", 0, 1, tokens=10, cost=0.1, name="root"),
         make_obs("b", 1, 2, level="ERROR", tokens=5, cost=0.2, name="tool")],
    )  # fmt: skip
    assert out.observation_count == 2
    assert out.latency_ms == 3000.0
    assert (out.total_tokens, out.max_level) == (15, "ERROR")
    assert out.total_cost == pytest.approx(0.3)
    assert out.search_text == 'chat {"q": "hello"} bye root tool'
    empty = ch.compute_aggregates(TraceRow(id="x", timestamp=T0), [])
    assert (empty.latency_ms, empty.max_level, empty.observation_count) == (None, "DEFAULT", 0)
    long = ch.compute_aggregates(TraceRow(id="x", timestamp=T0, output="y" * 9000), [])
    assert len(long.search_text) == ch.SEARCH_TEXT_LIMIT


def test_insert_row_shape_matches_columns_and_json_text():
    client = FakeClient()
    trace = TraceRow(id="t1", timestamp=T0, input={"a": 1}, metadata={"k": "v"}, tags=["x"])
    ClickHouseTraceStore(client, "db").upsert(PID, [trace], [], [])
    table, data, columns = client.inserts[0]
    assert table == "db.traces" and columns == ch.TRACE_COLUMNS
    row = dict(zip(columns, data[0], strict=True))
    assert row["project_id"] == PID and row["tags"] == ["x"]
    assert (row["input"], row["output"], row["metadata"]) == ('{"a": 1}', None, '{"k": "v"}')
    assert row["event_ts"].tzinfo is not None
    assert len(data[0]) == len(columns)


def test_upsert_merges_with_stored_row_by_event_ts():
    stored = trace_record("t1", name="old", output='"kept"', event_ts=T0, latency_ms=9.0)
    client = FakeClient({"FROM db.traces": as_result([stored], ch.TRACE_COLUMNS)})
    newer = TraceRow(id="t1", timestamp=T0, name="new", event_ts=T0 + timedelta(1))
    ClickHouseTraceStore(client, "db").upsert(PID, [newer], [], [])
    row = dict(zip(ch.TRACE_COLUMNS, client.inserts[0][1][0], strict=True))
    assert row["name"] == "new"  # newer wins
    assert row["output"] == '"kept"'  # unset incoming never erases
    assert row["latency_ms"] == 9.0 and row["bookmarked"] is True  # derived/flags are kept
    older = TraceRow(id="t1", timestamp=T0, name="ancient", user_id="u", event_ts=T0 - timedelta(1))
    client.inserts.clear()
    ClickHouseTraceStore(client, "db").upsert(PID, [older], [], [])
    row = dict(zip(ch.TRACE_COLUMNS, client.inserts[0][1][0], strict=True))
    assert (row["name"], row["user_id"]) == ("old", "u")  # older only fills gaps


def test_get_existing_is_scoped_by_kind():
    client = FakeClient({"FROM db.traces": as_result([trace_record("t1")], ch.TRACE_COLUMNS)})
    found = ClickHouseTraceStore(client, "db").get_existing(
        PID, {"trace": ["t1", "t2"], "score": []}
    )
    assert list(found["trace"]) == ["t1"] and found["score"] == {}
    sql, params = client.queries[0]
    assert_project_scoped(sql, params)
    assert params["ids"] == ["t1", "t2"]
    with pytest.raises(ValueError):
        ClickHouseTraceStore(client, "db").get_existing(PID, {"user": ["x"]})


def test_refresh_aggregates_reinserts_trace_with_same_event_ts():
    obs = dict.fromkeys(ch.OBSERVATION_COLUMNS, None)
    obs.update(id="o1", trace_id="t1", start_time=T0, end_time=T0 + timedelta(seconds=2),
               metadata="{}", model_parameters="{}", usage_details='{"total": 7}',
               cost_details='{"total": 0.7}', type="span", level="ERROR", name="step")  # fmt: skip
    client = FakeClient(
        {
            "FROM db.traces": as_result([trace_record("t1", latency_ms=None)], ch.TRACE_COLUMNS),
            "FROM db.observations": as_result([obs], ch.OBSERVATION_COLUMNS),
        }
    )
    ClickHouseTraceStore(client, "db").refresh_trace_aggregates(PID, ["t1"])
    row = dict(zip(ch.TRACE_COLUMNS, client.inserts[0][1][0], strict=True))
    assert (row["latency_ms"], row["total_tokens"], row["max_level"]) == (2000.0, 7, "ERROR")
    assert row["event_ts"] == T0
    ClickHouseTraceStore(client, "db").refresh_trace_aggregates(PID, [])


def test_update_flags_and_delete():
    client = FakeClient({"FROM db.traces": as_result([trace_record("t1")], ch.TRACE_COLUMNS)})
    store = ClickHouseTraceStore(client, "db")
    assert store.update_trace_flags(PID, "t1", bookmarked=False, tags=["x", "y"]) is True
    row = dict(zip(ch.TRACE_COLUMNS, client.inserts[0][1][0], strict=True))
    assert (row["bookmarked"], row["tags"]) == (False, ["x", "y"])
    with pytest.raises(ValueError):
        store.update_trace_flags(PID, "t1", name="no")
    assert (
        ClickHouseTraceStore(FakeClient(), "db").update_trace_flags(PID, "zzz", public=True)
        is False
    )

    client.commands.clear()
    assert store.delete_traces(PID, ["t1", "ghost"]) == 1
    deletes = [c for c in client.commands]
    assert [c[0].split(" WHERE ")[0] for c in deletes] == [
        "DELETE FROM db.traces", "DELETE FROM db.observations", "DELETE FROM db.scores"
    ]  # fmt: skip
    for sql, params in deletes:
        assert_project_scoped(sql, params)
        assert params["ids"] == ["t1"]
    # delete by query selects ids first, scoped and filtered
    client2 = FakeClient({"SELECT id FROM": (["id"], [["a"], ["b"]])})
    assert ClickHouseTraceStore(client2, "db").delete_traces(PID, TraceQuery(name="x")) == 2
    assert_project_scoped(*client2.queries[0])
    assert "name = {name:String}" in client2.queries[0][0]


<<<<<<< HEAD
def test_add_score_inserts_one_score_row():
    from traceloom.ports import ScoreRow

    client = FakeClient()
    ClickHouseTraceStore(client, "db").add_score(
        PID,
        ScoreRow(id="s1", name="q", timestamp=T0, trace_id="t1", value=1.0, source="ANNOTATION"),
    )
    ((table, data, columns),) = client.inserts
    assert table == "db.scores"
    row = dict(zip(columns, data[0], strict=True))
    assert (row["project_id"], row["trace_id"], row["source"]) == (PID, "t1", "ANNOTATION")


def test_matching_trace_ids_is_scoped_and_filtered():
    client = FakeClient({"SELECT id FROM": (["id"], [["a"], ["b"]])})
    ids = ClickHouseTraceStore(client, "db").matching_trace_ids(PID, TraceQuery(name="drop"))
    assert ids == ["a", "b"]
    sql, params = client.queries[0]
    assert_project_scoped(sql, params)
    assert "name = {name:String}" in sql and params["name"] == "drop"
=======
def test_stats_and_environment_queries_are_project_scoped_and_bound():
    from traceloom.adapters.full.clickhouse_trace_store import build_environments, build_trace_stats
    from traceloom.ports import TraceQuery

    sql, params = build_trace_stats("p1", TraceQuery(user_id="u1", environment="prod"))
    assert "FROM traces FINAL" in sql and "project_id = {project_id:String}" in sql
    assert "countIf(max_level = 'ERROR')" in sql and "quantileExact(0.95)(latency_ms)" in sql
    assert params["project_id"] == "p1" and params["user_id"] == "u1" and "u1" not in sql
    sql, params = build_environments("p1")
    assert "SELECT DISTINCT environment" in sql and "project_id = {project_id:String}" in sql
    assert params == {"project_id": "p1"}
>>>>>>> team/2026-11-06-phase1-tracing-core
