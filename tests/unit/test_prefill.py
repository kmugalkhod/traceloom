"""Prefill keeps tool-call ids, and pairs results with calls when a trace recorded none."""

from __future__ import annotations

from traceloom.llm.prefill import messages_from


def test_ids_survive_openai_shape():
    out = messages_from([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
    ])  # fmt: skip
    assert out[1]["tool_calls"][0]["id"] == "c1" and out[2]["tool_call_id"] == "c1"


def test_missing_ids_are_synthesised_and_paired_in_order():
    out = messages_from([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"type": "function", "function": {"name": "a", "arguments": "{}"}},
            {"id": "", "type": "function", "function": {"name": "b", "arguments": "{}"}}]},
        {"role": "tool", "content": "ra"},
        {"role": "tool", "tool_call_id": "", "content": "rb"},
    ])  # fmt: skip
    ids = [c["id"] for c in out[1]["tool_calls"]]
    assert all(ids) and len(set(ids)) == 2
    assert [m["tool_call_id"] for m in out[2:]] == ids


def test_missing_anthropic_ids_are_synthesised_on_both_sides():
    out = messages_from([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "f", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "r"}]},
    ])  # fmt: skip
    call_id = out[1]["tool_calls"][0]["id"]
    assert call_id and out[2] == {"role": "tool", "tool_call_id": call_id, "content": "r"}


def test_a_tool_result_with_no_call_to_answer_becomes_a_user_message():
    """Old demo traces: `assistant: calling search` then a bare tool message."""
    out = messages_from([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "calling search"},
        {"role": "tool", "content": "search -> ok"},
    ])
    assert out[2]["role"] == "user" and "search -> ok" in out[2]["content"]
    assert all(m["role"] != "tool" for m in out)
