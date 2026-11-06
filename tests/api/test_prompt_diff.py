from __future__ import annotations

from markupsafe import Markup

from traceloom.prompts.diff import diff_chat, diff_config, diff_text, diff_versions


def ops(lines):
    return [(line.op, str(line.text)) for line in lines]


def test_text_diff_lines():
    out = diff_text("a\nb\nc", "a\nB\nc\nd")
    assert ops(out) == [
        ("equal", "a"),
        ("remove", "b"),
        ("add", "B"),
        ("equal", "c"),
        ("add", "d"),
    ]
    assert (out[0].old_no, out[0].new_no) == (1, 1)
    assert (out[1].old_no, out[1].new_no) == (2, None)
    assert (out[2].old_no, out[2].new_no) == (None, 2)


def test_identical_and_empty_text():
    assert all(line.op == "equal" for line in diff_text("x\ny", "x\ny"))
    assert diff_text("", "") == []
    assert ops(diff_text("", "new")) == [("add", "new")]
    assert ops(diff_text("old", "")) == [("remove", "old")]


def test_chat_diff_goes_message_by_message():
    old = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "{{q}}"},
        {"role": "assistant", "content": "bye"},
    ]
    new = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Question: {{q}}"},
        {"role": "assistant", "content": "bye"},
        {"role": "user", "content": "more"},
    ]
    out = diff_chat(old, new)
    assert [(m.index, m.status) for m in out] == [
        (0, "unchanged"),
        (1, "changed"),
        (2, "unchanged"),
        (3, "added"),
    ]
    assert ops(out[1].lines) == [("remove", "{{q}}"), ("add", "Question: {{q}}")]
    assert out[3].new_role == "user" and out[3].old_role is None


def test_chat_removed_message_and_role_change():
    old = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
    new = [{"role": "system", "content": "x"}]
    out = diff_chat(old, new)
    assert out[0].status == "changed" and out[0].role_changed
    assert out[1].status == "removed" and ops(out[1].lines) == [("remove", "y")]


def test_config_diff_is_key_level():
    out = diff_config(
        {"model": "a", "temperature": 0.1, "stop": ["x"]},
        {"model": "b", "temperature": 0.1, "max_tokens": 10},
    )
    by_key = {str(k.key): k for k in out}
    assert {k: v.status for k, v in by_key.items()} == {
        "max_tokens": "added",
        "model": "changed",
        "stop": "removed",
        "temperature": "unchanged",
    }
    # json.dumps output is escaped, so quotes appear as &#34;
    assert "a" in str(by_key["model"].old) and "b" in str(by_key["model"].new)
    assert by_key["max_tokens"].old is None and str(by_key["max_tokens"].new) == "10"


def test_nested_config_change_is_one_key():
    out = diff_config({"tools": [{"a": 1}]}, {"tools": [{"a": 2}]})
    assert [(str(k.key), k.status) for k in out] == [("tools", "changed")]


def test_output_carries_no_raw_html():
    evil = "<script>alert(1)</script><img src=x onerror=1> & q"
    out = diff_versions(
        "chat",
        [{"role": "user<b>", "content": "ok"}],
        [{"role": "user<b>", "content": evil}],
        {"<i>k</i>": "<u>v</u>"},
        {},
    )
    rendered = repr(out)
    for raw in ("<script", "<img", "<b>", "<i>", "<u>"):
        assert raw not in rendered
    line = out.content[0].lines[1]
    assert isinstance(line.text, Markup)
    assert "&lt;script&gt;" in line.text and "&amp;" in line.text
    assert str(out.config[0].key) == "&lt;i&gt;k&lt;/i&gt;"


def test_text_output_escaped():
    out = diff_text("a", "<b>x</b>")
    assert all("<" not in str(line.text) for line in out)


def test_diff_versions_changed_flag():
    same = diff_versions("text", "a", "a", {"m": 1}, {"m": 1})
    assert not same.changed
    assert diff_versions("text", "a", "b", {}, {}).changed
    assert diff_versions("text", "a", "a", {}, {"m": 1}).changed
    assert diff_versions("chat", [{"role": "user", "content": "a"}], [], {}, {}).changed
