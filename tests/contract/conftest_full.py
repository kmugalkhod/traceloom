"""ClickHouse side of the shared store contract suite. Active only with TRACELOOM_IT=1.

The default test run never imports clickhouse_connect or opens a socket from here.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

IT_ENABLED = os.environ.get("TRACELOOM_IT") == "1"
CLICKHOUSE_PARAM = pytest.param("clickhouse", marks=pytest.mark.it)


def open_clickhouse_store() -> Iterator:
    """Yield a ClickHouseTraceStore on a throwaway database, dropped afterwards."""
    import clickhouse_connect

    from traceloom.adapters.full.clickhouse_trace_store import ClickHouseTraceStore, apply_schema

    client = clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
    )
    database = f"traceloom_test_{uuid.uuid4().hex[:10]}"
    apply_schema(client, database)
    try:
        yield ClickHouseTraceStore(client, database)
    finally:
        client.command(f"DROP DATABASE IF EXISTS {database}")
        client.close()
