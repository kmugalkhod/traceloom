"""Read-side queries for the lite SQL TraceStore (filled in by the read-path task)."""

from __future__ import annotations


class SqlReadMixin:
    """list_traces, get_trace, sessions and users. Mixed into SqlTraceStore."""
