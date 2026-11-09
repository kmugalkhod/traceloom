import pytest
from sqlalchemy.exc import IntegrityError

from traceloom.db.models_eval import EvalExecution, EvalTemplate, EvalTemplateVersion


def test_factories_produce_valid_rows(db, world, make_eval_template, make_eval_rule):
    template = make_eval_template(world.project, "helpfulness")
    rule = make_eval_rule(world.project, template)
    assert template.latest.version == 1 and template.latest.params == {}
    assert (rule.enabled, rule.sampling_pct, rule.delay_seconds) == (True, 100.0, 30)
    assert rule.score_name == "helpfulness" and rule.template_version is None


def test_builtin_template_has_no_project(db, make_eval_template):
    builtin = make_eval_template(None, "hallucination")
    assert db.get(EvalTemplate, builtin.id).project_id is None


def test_execution_is_unique_per_rule_and_object(db, world, make_eval_rule, make_eval_execution):
    rule, other = make_eval_rule(world.project), make_eval_rule(world.project)
    execution = make_eval_execution(rule, "t1")
    assert (execution.status, execution.attempts, execution.estimated_cost) == ("pending", 0, 0.0)
    make_eval_execution(rule, "t1", object_type="observation")  # another object type is fine
    make_eval_execution(other, "t1")  # another rule is fine
    with pytest.raises(IntegrityError):
        make_eval_execution(rule, "t1")
    db.rollback()
    assert db.query(EvalExecution).filter_by(rule_id=rule.id).count() == 2


def test_executions_without_a_rule_are_not_constrained(db, world, make_eval_rule):
    rule = make_eval_rule(world.project)
    version = db.query(EvalTemplateVersion).filter_by(template_id=rule.template_id).one()
    for _ in range(2):  # experiment executions: run_item_id set, no rule
        db.add(
            EvalExecution(
                project_id=world.project.id,
                run_item_id="ri1",
                template_version_id=version.id,
                object_type="trace",
                object_id="t1",
            )
        )
        db.commit()
    assert db.query(EvalExecution).count() == 2
