from __future__ import annotations

import pytest
from markupsafe import Markup

from traceloom.web.render import (
    is_chat,
    pretty_json,
    raw_json,
    render_markdown,
    render_value,
)

XSS = "<script>alert(1)</script>"


def test_pretty_json_indents_structures_and_passes_strings_through():
    assert pretty_json({"a": [1, 2]}) == '{\n  "a": [\n    1,\n    2\n  ]\n}'
    assert pretty_json("plain") == "plain"
    assert pretty_json(None) == ""
    assert pretty_json({"é": 1}) == '{\n  "é": 1\n}'


def test_raw_json_is_compact():
    assert raw_json({"a": [1, 2]}) == '{"a": [1, 2]}'


def test_chat_detection():
    chat = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    assert is_chat(chat)
    assert not is_chat([])
    assert not is_chat([{"role": "user"}])
    assert not is_chat([{"role": "user", "content": "x"}, "nope"])
    assert not is_chat({"role": "user", "content": "x"})
    assert not is_chat("text")


def test_list_of_messages_renders_as_chat_and_other_values_pretty_print():
    chat = render_value([{"role": "user", "content": "hi"}, {"role": "weird", "content": {"a": 1}}])
    assert chat.kind == "chat"
    assert [m.css_role for m in chat.messages] == ["user", "other"]
    assert "&#34;a&#34;" in chat.messages[1].html
    other = render_value({"k": "v"})
    assert other.kind == "text"
    assert '"k": "v"' in other.html.unescape()
    assert render_value(None).kind == "empty"
    assert render_value({}).kind == "empty"


def test_script_is_escaped_in_every_mode():
    values = [
        XSS,
        {"k": XSS},
        [{"role": XSS, "content": XSS}],
    ]
    for value in values:
        for mode in ("formatted", "json", "markdown"):
            rendered = render_value(value, mode)
            html = str(rendered.html) + "".join(f"{m.role}{m.html}" for m in rendered.messages)
            if rendered.kind == "chat":
                # role is raw text for the template to escape; content is already escaped
                assert "<script>" not in "".join(str(m.html) for m in rendered.messages)
                continue
            assert "<script>" not in html, (mode, value)
            assert "&lt;script&gt;" in html, (mode, value)
    assert isinstance(render_value(XSS).html, Markup)


def test_chat_role_is_not_usable_as_a_css_class_injection():
    msg = render_value([{"role": '"><script>', "content": "x"}]).messages[0]
    assert msg.css_role == "other"


def test_markdown_renders_basic_formatting():
    html = render_markdown("**bold** and `code`\n\n- a\n- b")
    assert "<strong>bold</strong>" in html and "<code>code</code>" in html and "<li>a</li>" in html


def test_markdown_disables_raw_html_and_dangerous_links():
    html = str(
        render_markdown(
            f"{XSS} <img src=x onerror=alert(1)> [click](javascript:alert(1)) "
            "[data](data:text/html;base64,AAAA) [ok](https://example.com) ![pic](http://e/x.png)"
        )
    )
    assert "<script>" not in html and "<img" not in html
    assert 'href="javascript' not in html and 'href="data' not in html
    assert 'href="https://example.com"' in html
    assert 'rel="noopener noreferrer nofollow"' in html


def test_markdown_of_non_text_values_is_escaped_json():
    assert render_markdown({"k": XSS}) == Markup(
        "<pre>{\n  &#34;k&#34;: &#34;&lt;script&gt;alert(1)&lt;/script&gt;&#34;\n}</pre>"
    )
    assert render_markdown(None) == ""


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        render_value("x", "html")
