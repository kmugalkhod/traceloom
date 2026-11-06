from __future__ import annotations

import pytest

from traceloom.prompts.composition import (
    MAX_DEPTH,
    CompositionError,
    Reference,
    Resolved,
    find_references,
    parse_reference,
    resolve_content,
    resolve_messages,
    resolve_text,
)
from traceloom.prompts.variables import (
    compile_messages,
    compile_text,
    find_message_variables,
    find_variables,
)


def store(**prompts: tuple[int, str, object]):
    """A resolver over {name: (version, type, content)}; a pinned version wins over the entry."""

    def resolve(ref: Reference) -> Resolved | None:
        if ref.name not in prompts:
            return None
        version, ptype, content = prompts[ref.name]
        return Resolved(ref.name, ref.version or version, ptype, content, ref.label)

    return resolve


# --- variables ---------------------------------------------------------------------------


def test_find_variables_distinct_in_order():
    assert find_variables("Hi {{name}}, {{ topic }} and {{name}} again {{1bad}} {x}") == [
        "name",
        "topic",
    ]


def test_compile_substitutes_known_and_reports_missing_without_raising():
    result = compile_text("Hi {{name}}, about {{topic}} {{topic}}", {"name": "Ann", "extra": 1})
    assert result.text == "Hi Ann, about {{topic}} {{topic}}"
    assert result.missing == ["topic"]


def test_compile_does_not_rescan_substituted_values():
    result = compile_text("{{a}}", {"a": "{{b}}", "b": "boom"})
    assert result.text == "{{b}}" and result.missing == []


def test_compile_coerces_non_strings():
    assert compile_text("n={{n}}", {"n": 3}).text == "n=3"


def test_compile_messages_skips_placeholders_and_collects_missing():
    messages = [
        {"role": "system", "content": "You help {{user}}"},
        {"role": "placeholder", "content": "{{history}}"},
        {"role": "user", "content": "{{q}} {{user}}"},
    ]
    out = compile_messages(messages, {"user": "Ann"})
    assert [m["content"] for m in out.messages] == ["You help Ann", "{{history}}", "{{q}} Ann"]
    assert out.missing == ["q"]
    assert messages[0]["content"] == "You help {{user}}"  # input untouched
    assert find_message_variables(messages) == ["user", "q"]


# --- composition -------------------------------------------------------------------------


def test_parse_reference_forms():
    by_label = parse_reference("@@@prompt:support/tone|label=production@@@")
    assert (by_label.name, by_label.label, by_label.version) == ("support/tone", "production", None)
    by_version = parse_reference("@@@prompt:tone|version=3@@@")
    assert (by_version.label, by_version.version) == (None, 3)


@pytest.mark.parametrize(
    "raw",
    [
        "@@@prompt:tone@@@",
        "@@@prompt:tone|label=@@@",
        "@@@prompt:tone|version=0@@@",
        "@@@prompt:tone|version=x@@@",
        "@@@prompt:bad name|label=a@@@",
        "@@@prompt:tone|latest=1@@@",
    ],
)
def test_malformed_reference_names_the_reference(raw):
    with pytest.raises(CompositionError) as err:
        resolve_text(f"before {raw} after", store())
    assert raw in str(err.value) and err.value.reference == raw


def test_find_references():
    refs = find_references("a @@@prompt:x|label=a@@@ b @@@prompt:y|version=2@@@")
    assert [(r.name, r.label, r.version) for r in refs] == [("x", "a", None), ("y", None, 2)]


def test_resolve_replaces_and_records_dependency_versions():
    resolver = store(tone=(4, "text", "Be kind."))
    text, deps = resolve_text("Rules: @@@prompt:tone|label=production@@@", resolver)
    assert text == "Rules: Be kind."
    assert deps == [{"name": "tone", "version": 4, "label": "production"}]


def test_pinned_version_is_recorded():
    resolver = store(tone=(4, "text", "Be kind."))
    _, deps = resolve_text("@@@prompt:tone|version=2@@@", resolver)
    assert deps == [{"name": "tone", "version": 2, "label": None}]


def test_nested_resolution_lists_every_dependency_once():
    resolver = store(
        a=(1, "text", "A[@@@prompt:b|label=x@@@]"),
        b=(2, "text", "B[@@@prompt:c|label=x@@@]"),
        c=(3, "text", "C"),
    )
    text, deps = resolve_text(
        "@@@prompt:a|label=x@@@ @@@prompt:c|label=x@@@ @@@prompt:a|label=x@@@", resolver
    )
    assert text == "A[B[C]] C A[B[C]]"
    assert [(d["name"], d["version"]) for d in deps] == [("a", 1), ("b", 2), ("c", 3)]


def test_cycle_raises_naming_the_reference():
    resolver = store(
        a=(1, "text", "@@@prompt:b|label=x@@@"), b=(1, "text", "@@@prompt:a|label=x@@@")
    )
    with pytest.raises(CompositionError, match="Circular") as err:
        resolve_text("@@@prompt:a|label=x@@@", resolver)
    assert err.value.reference == "@@@prompt:a|label=x@@@"
    assert "@@@prompt:a|label=x@@@" in str(err.value)


def test_self_reference_through_root_is_a_cycle():
    resolver = store(a=(1, "text", "me"))
    with pytest.raises(CompositionError, match="Circular"):
        resolve_text("hello @@@prompt:a|version=1@@@", resolver, root=("a", 1))


def chain(length: int):
    prompts = {
        f"p{i}": (1, "text", f"@@@prompt:p{i + 1}|label=x@@@" if i < length else "end")
        for i in range(1, length + 1)
    }
    return store(**prompts)


def test_depth_five_is_allowed():
    text, deps = resolve_text("@@@prompt:p1|label=x@@@", chain(MAX_DEPTH))
    assert text == "end" and len(deps) == MAX_DEPTH


def test_depth_above_five_errors():
    with pytest.raises(CompositionError, match="deeper than 5"):
        resolve_text("@@@prompt:p1|label=x@@@", chain(MAX_DEPTH + 1))


def test_depth_limit_is_not_shifted_by_root():
    text, _ = resolve_text("@@@prompt:p1|label=x@@@", chain(MAX_DEPTH), root=("top", 1))
    assert text == "end"
    with pytest.raises(CompositionError, match="deeper than 5"):
        resolve_text("@@@prompt:p1|label=x@@@", chain(MAX_DEPTH + 1), root=("top", 1))


def test_missing_reference_errors_naming_it():
    with pytest.raises(CompositionError, match="not found") as err:
        resolve_text("@@@prompt:ghost|label=x@@@", store())
    assert err.value.reference == "@@@prompt:ghost|label=x@@@"


def test_chat_prompts_cannot_be_included():
    resolver = store(chatty=(1, "chat", [{"role": "user", "content": "hi"}]))
    with pytest.raises(CompositionError, match="Only text prompts"):
        resolve_text("@@@prompt:chatty|label=x@@@", resolver)


def test_references_inside_chat_message_content_are_resolved():
    resolver = store(tone=(2, "text", "Be kind."))
    messages = [
        {"role": "system", "content": "Rules: @@@prompt:tone|label=x@@@"},
        {"role": "placeholder", "content": "history"},
        {"role": "user", "content": "{{q}}"},
    ]
    out, deps = resolve_messages(messages, resolver)
    assert out[0]["content"] == "Rules: Be kind."
    assert out[2]["content"] == "{{q}}"
    assert messages[0]["content"].startswith("Rules: @@@")  # input untouched
    assert deps == [{"name": "tone", "version": 2, "label": "x"}]


def test_resolve_content_dispatches_on_type():
    resolver = store(tone=(1, "text", "T"))
    assert resolve_content("text", "@@@prompt:tone|label=x@@@", resolver)[0] == "T"
    msgs = [{"role": "user", "content": "@@@prompt:tone|label=x@@@"}]
    assert resolve_content("chat", msgs, resolver)[0][0]["content"] == "T"


# --- fan-out limits ------------------------------------------------------------------------


def _fan_out(width: int, levels: int):
    """{lN: (1, text, width refs to lN+1)}, ending in a short leaf."""
    prompts = {f"l{levels}": (1, "text", "leaf ")}
    for level in range(levels - 1, -1, -1):
        prompts[f"l{level}"] = (1, "text", f"@@@prompt:l{level + 1}|label=production@@@" * width)
    return prompts


def test_include_fan_out_is_capped_quickly():
    import time

    from traceloom.prompts.composition import MAX_EXPANSIONS

    started = time.monotonic()
    with pytest.raises(CompositionError, match=f"more than {MAX_EXPANSIONS} includes"):
        resolve_text(
            "@@@prompt:l0|label=production@@@", store(**_fan_out(width=20, levels=4))
        )  # five levels of 20 includes: 20**5 expansions uncapped
    assert time.monotonic() - started < 1


def test_expanded_size_is_capped():
    from traceloom.prompts.composition import MAX_EXPANDED_CHARS

    big = "x" * (MAX_EXPANDED_CHARS // 4 + 1)
    with pytest.raises(CompositionError, match="longer than"):
        resolve_text("@@@prompt:big|label=production@@@" * 4, store(big=(1, "text", big)))


def test_lookups_are_memoised_per_resolution():
    calls = []
    inner = store(**_fan_out(width=3, levels=2))

    def counting(ref):
        calls.append(ref.raw)
        return inner(ref)

    text, deps = resolve_text("@@@prompt:l0|label=production@@@" * 2, counting)
    assert text == "leaf " * 18
    assert sorted(set(calls)) == sorted(calls)  # each distinct reference looked up once
    assert [d["name"] for d in deps] == ["l0", "l1", "l2"]


def test_normal_includes_within_the_limits_still_work():
    text, _ = resolve_text(
        "@@@prompt:l0|label=production@@@", store(**_fan_out(width=4, levels=3))
    )
    assert text == "leaf " * 64
