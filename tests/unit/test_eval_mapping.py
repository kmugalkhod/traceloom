import pytest

from traceloom.evals.mapping import (
    MappingError,
    ObjectContext,
    PathError,
    parse_path,
    resolve_path,
    resolve_variables,
)

DOC = {
    "a": {"b": [10, {"c": "deep"}]},
    "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ],
    "my-key": 1,
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("$", DOC),
        ("$.a.b[0]", 10),
        ("$.a.b[1].c", "deep"),
        ("$.a.b[-1].c", "deep"),
        ("$.messages[-1].content", "hello"),
        ("$.messages[0].role", "user"),
        ("$.my-key", 1),
    ],
)
def test_path_resolves_nested_and_negative_indexes(path, expected):
    assert resolve_path(DOC, path) == expected


@pytest.mark.parametrize("path", ["$.a.x", "$.a.b[5]", "$.a.b[-3]", "$.messages.role", "$.a[0]"])
def test_missing_path_raises_a_clear_error(path):
    with pytest.raises(MappingError, match="not found"):
        resolve_path(DOC, path)


@pytest.mark.parametrize(
    "path", ["a.b", "$..a", "$.a[*]", "$.a[?(@.x)]", "$['a']", "$.a b", "$.", "$[x]", "$.a()"]
)
def test_only_the_subset_is_accepted_and_nothing_is_evaluated(path):
    with pytest.raises(PathError):
        parse_path(path)
    with pytest.raises(MappingError):
        resolve_path(DOC, path)


def test_a_json_document_stored_as_text_is_walked():
    assert resolve_path('{"x": [1, 2]}', "$.x[-1]") == 2


def ctx(**kw):
    return ObjectContext(
        input=kw.get("input", {"messages": [{"content": "q1"}, {"content": "q2"}]}),
        output=kw.get("output", "the answer"),
        metadata=kw.get("metadata", {"tier": "pro"}),
        expected_output=kw.get("expected_output"),
        observations=kw.get(
            "observations",
            [
                {"name": "retrieve", "input": "q", "output": {"docs": ["d1", "d2"]}},
                {"name": "retrieve", "input": "q", "output": {"docs": ["d3"]}},
            ],
        ),
    )


def test_variables_resolve_from_every_source():
    mapping = {
        "question": {"source": "input", "path": "$.messages[-1].content"},
        "tier": {"source": "metadata.tier"},
        "docs": {"source": "observation", "observation": "retrieve", "field": "output"},
        "first_doc": {
            "source": "observation",
            "observation": "retrieve",
            "path": "$.docs[0]",
        },
    }
    values = resolve_variables(["question", "output", "tier", "docs", "first_doc"], mapping, ctx())
    assert values["question"] == "q2"
    assert values["output"] == "the answer"  # implicit mapping
    assert values["tier"] == "pro"
    assert values["docs"] == '{"docs": ["d3"]}'  # the latest of repeated observations, as JSON
    assert values["first_doc"] == "d3"


def test_a_missing_variable_raises_naming_it():
    with pytest.raises(MappingError, match="variable 'tone'.*no mapping"):
        resolve_variables(["tone"], {}, ctx())
    with pytest.raises(MappingError, match="variable 'x'.*no key 'nope'"):
        resolve_variables(["x"], {"x": {"source": "metadata.nope"}}, ctx())
    with pytest.raises(MappingError, match="variable 'expected_output'.*empty"):
        resolve_variables(["expected_output"], {}, ctx())
    with pytest.raises(MappingError, match="variable 'x'.*no child observation named 'gone'"):
        resolve_variables(["x"], {"x": {"source": "observation", "observation": "gone"}}, ctx())
    with pytest.raises(MappingError, match="unknown source"):
        resolve_variables(["x"], {"x": {"source": "os.environ"}}, ctx())


def test_a_default_is_used_when_the_value_is_missing():
    mapping = {
        "a": {"source": "metadata.nope", "default": "n/a"},
        "b": {"source": "input", "path": "$.nope", "default": ""},
        "c": {"source": "expected_output", "default": {"k": 1}},
        "d": {"source": "metadata.tier", "default": "ignored"},
    }
    assert resolve_variables(["a", "b", "c", "d"], mapping, ctx()) == {
        "a": "n/a",
        "b": "",
        "c": '{"k": 1}',
        "d": "pro",
    }
