import pytest
from sqlalchemy import select

from traceloom.db.models_eval import EvalTemplate, EvalTemplateVersion
from traceloom.evals import templates as tpl
from traceloom.evals.templates import BuiltinReadOnly, TemplateError, TemplateNotFound

SCHEMA = {"type": "numeric", "min": 0, "max": 1}


@pytest.fixture
def pid(world):
    return world.project.id


def test_each_save_creates_a_new_immutable_version(db, pid):
    view = tpl.create_template(db, pid, "tone", "Rate {{output}}", SCHEMA, model="gpt-4o-mini")
    assert view.latest.version == 1
    second = tpl.save_version(db, pid, view.template.id, "Rate politely: {{output}}", SCHEMA)
    third = tpl.save_version(db, pid, view.template.id, "Rate: {{output}}", {"type": "boolean"})
    db.commit()
    assert (second.version, third.version) == (2, 3)
    rows = tpl.versions(db, view.template.id)
    assert [r.version for r in rows] == [3, 2, 1]
    assert rows[2].prompt == "Rate {{output}}" and rows[2].model == "gpt-4o-mini"  # untouched
    assert tpl.get_version(db, view.template.id).version == 3  # latest
    assert tpl.get_version(db, view.template.id, 1).prompt == "Rate {{output}}"
    with pytest.raises(TemplateNotFound):
        tpl.get_version(db, view.template.id, 9)


def test_builtins_are_seeded_once_listed_and_read_only(db, pid):
    assert tpl.seed_builtins(db) == 0  # startup already seeded them; running again is a no-op
    db.commit()
    views = tpl.list_templates(db, pid)
    assert {v.template.name for v in views} == {
        "hallucination",
        "relevance",
        "toxicity",
        "helpfulness",
        "correctness",
        "conciseness",
    }
    assert all(v.builtin and v.latest.model is None for v in views)
    correctness = next(v for v in views if v.template.name == "correctness")
    assert correctness.variables == ["input", "expected_output", "output"]
    with pytest.raises(BuiltinReadOnly):
        tpl.save_version(db, pid, correctness.template.id, "changed {{output}}", SCHEMA)
    assert tpl.get_version(db, correctness.template.id).version == 1


def test_a_builtin_can_be_cloned_and_the_clone_edited(db, pid):
    builtin = db.scalar(select(EvalTemplate).where(EvalTemplate.name == "toxicity"))
    copy = tpl.clone(db, pid, builtin.id, created_by="u1")
    assert copy.template.project_id == pid and not copy.builtin
    assert copy.template.name == "toxicity (copy)" and copy.latest.created_by == "u1"
    assert copy.latest.prompt == tpl.get_version(db, builtin.id).prompt
    assert tpl.save_version(db, pid, copy.template.id, "Mine {{output}}", SCHEMA).version == 2
    with pytest.raises(TemplateError, match="already exists"):
        tpl.clone(db, pid, builtin.id)
    assert tpl.clone(db, pid, builtin.id, "my toxicity").template.name == "my toxicity"


def test_templates_are_project_scoped(db, pid, make_org, make_project):
    other = make_project(make_org())
    mine = tpl.create_template(db, pid, "mine", "x {{input}}", SCHEMA)
    db.commit()
    assert [v.template.name for v in tpl.list_templates(db, other.id) if not v.builtin] == []
    with pytest.raises(TemplateNotFound):
        tpl.get_template(db, other.id, mine.template.id)
    with pytest.raises(TemplateNotFound):
        tpl.save_version(db, other.id, mine.template.id, "x", SCHEMA)
    # the same name is fine in another project
    tpl.create_template(db, other.id, "mine", "x {{input}}", SCHEMA)


@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"type": "text"},
        {"type": "numeric"},
        {"type": "numeric", "min": 1, "max": 1},
        {"type": "numeric", "min": "0", "max": 1},
        {"type": "categorical", "categories": ["only"]},
        {"type": "categorical", "categories": ["a", "a"]},
        {"type": "categorical", "categories": ["a", ""]},
    ],
)
def test_invalid_output_schemas_are_rejected(db, pid, schema):
    with pytest.raises(TemplateError):
        tpl.create_template(db, pid, "bad", "x {{input}}", schema)


def test_prompt_and_name_are_validated(db, pid):
    with pytest.raises(TemplateError, match="prompt is required"):
        tpl.create_template(db, pid, "n", "  ", SCHEMA)
    with pytest.raises(TemplateError, match="name is required"):
        tpl.create_template(db, pid, " ", "x", SCHEMA)
    assert db.scalar(select(EvalTemplateVersion).where(EvalTemplateVersion.prompt == "")) is None
    assert db.query(EvalTemplate).filter_by(project_id=pid).count() == 0  # nothing half-saved


def test_the_starter_file_is_valid_and_original():
    entries = tpl.load_starter()
    assert [e["name"] for e in entries] == [
        "hallucination",
        "relevance",
        "toxicity",
        "helpfulness",
        "correctness",
        "conciseness",
    ]
