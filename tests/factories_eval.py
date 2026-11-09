"""Factories for eval templates, rules and executions, loaded as a pytest plugin by conftest."""

from __future__ import annotations

from collections.abc import Callable
from itertools import count

import pytest
from sqlalchemy.orm import Session

from traceloom.db.base import utcnow
from traceloom.db.models import Project
from traceloom.db.models_eval import EvalExecution, EvalRule, EvalTemplate, EvalTemplateVersion

_seq = count(1)


@pytest.fixture
def make_eval_template(db: Session) -> Callable[..., EvalTemplate]:
    """A template with one version; `.latest` holds that EvalTemplateVersion."""

    def make(project: Project | None, name: str | None = None, **version) -> EvalTemplate:
        template = EvalTemplate(
            project_id=project.id if project else None, name=name or f"template-{next(_seq)}"
        )
        db.add(template)
        db.flush()
        version.setdefault("prompt", "Rate this answer: {{output}}")
        version.setdefault("output_schema", {"type": "numeric", "min": 0, "max": 1})
        template.latest = EvalTemplateVersion(template_id=template.id, version=1, **version)  # type: ignore[attr-defined]
        db.add(template.latest)  # type: ignore[attr-defined]
        db.commit()
        return template

    return make


@pytest.fixture
def make_eval_rule(db: Session, make_eval_template) -> Callable[..., EvalRule]:
    def make(project: Project, template: EvalTemplate | None = None, **fields) -> EvalRule:
        template = template or make_eval_template(project)
        fields.setdefault("target", "trace")
        fields.setdefault("score_name", template.name)
        rule = EvalRule(project_id=project.id, template_id=template.id, **fields)
        db.add(rule)
        db.commit()
        return rule

    return make


@pytest.fixture
def make_eval_execution(db: Session) -> Callable[..., EvalExecution]:
    def make(rule: EvalRule, object_id: str | None = None, **fields) -> EvalExecution:
        template_version = fields.pop("template_version_id", None)
        if template_version is None:
            template_version = (
                db.query(EvalTemplateVersion).filter_by(template_id=rule.template_id).one().id
            )
        fields.setdefault("object_type", rule.target)
        object_id = object_id or f"trace-{next(_seq)}"
        fields.setdefault("trace_id", object_id)
        fields.setdefault("run_after", utcnow())
        execution = EvalExecution(
            project_id=rule.project_id,
            rule_id=rule.id,
            template_version_id=template_version,
            object_id=object_id,
            **fields,
        )
        db.add(execution)
        db.commit()
        return execution

    return make
