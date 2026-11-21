"""Phase 3 evaluation models: judge templates and versions, rules, executions, the project's
evaluation settings and the daily spend ledger."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from traceloom.db.base import Base, UTCDateTime, new_id, utcnow

EXECUTION_STATUSES = ("pending", "running", "succeeded", "failed", "skipped", "capped")


class EvalTemplate(Base):
    """A judge template. `project_id` is NULL for the built-in starter templates."""

    __tablename__ = "eval_templates"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_eval_templates_project_name"),
        Index("ix_eval_templates_project", "project_id"),
    )


class EvalTemplateVersion(Base):
    """One immutable save of a template."""

    __tablename__ = "eval_template_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    template_id: Mapped[str] = mapped_column(ForeignKey("eval_templates.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    prompt: Mapped[str] = mapped_column(Text)
    output_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    connection_id: Mapped[str | None] = mapped_column(String(32))  # None: project default
    model: Mapped[str | None] = mapped_column(String(200))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_eval_template_versions_version"),
    )


class EvalRule(Base):
    __tablename__ = "eval_rules"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200), default="")
    template_id: Mapped[str] = mapped_column(ForeignKey("eval_templates.id"))
    template_version: Mapped[int | None] = mapped_column(Integer)  # None: follow the latest
    # Model for this rule when the template version names none (built-ins never do).
    connection_id: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(200))
    target: Mapped[str] = mapped_column(String(16))  # trace | observation
    target_name: Mapped[str | None] = mapped_column(String(500))
    target_type: Mapped[str | None] = mapped_column(String(32))  # GENERATION, SPAN, EVENT, ...
    filter: Mapped[dict] = mapped_column(JSON, default=dict)
    sampling_pct: Mapped[float] = mapped_column(Float, default=100.0)
    delay_seconds: Mapped[int] = mapped_column(Integer, default=30)
    mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    score_name: Mapped[str] = mapped_column(String(200))
    config_id: Mapped[str | None] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_eval_rules_project_enabled", "project_id", "enabled"),)


class EvalExecution(Base):
    """One judge job: a rule firing on an object, or an evaluator run on an experiment item."""

    __tablename__ = "eval_executions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    rule_id: Mapped[str | None] = mapped_column(ForeignKey("eval_rules.id", ondelete="CASCADE"))
    run_item_id: Mapped[str | None] = mapped_column(String(32))  # dataset_run_items.id
    template_version_id: Mapped[str] = mapped_column(ForeignKey("eval_template_versions.id"))
    object_type: Mapped[str] = mapped_column(String(16))  # trace | observation
    object_id: Mapped[str] = mapped_column(String(200))
    trace_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime | None] = mapped_column(UTCDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    score_id: Mapped[str | None] = mapped_column(String(200))
    judge_trace_id: Mapped[str | None] = mapped_column(String(200))
    cost: Mapped[float | None] = mapped_column(Float)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        # One execution per (rule, object); executions without a rule (experiments) are exempt.
        Index(
            "uq_eval_executions_rule_object",
            "rule_id",
            "object_type",
            "object_id",
            unique=True,
            sqlite_where=text("rule_id IS NOT NULL"),
            postgresql_where=text("rule_id IS NOT NULL"),
        ),
        Index("ix_eval_executions_status_run_after", "status", "run_after"),
        Index("ix_eval_executions_project_created", "project_id", "created_at"),
    )


class ProjectEvalSettings(Base):
    __tablename__ = "project_eval_settings"
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    daily_cost_cap_usd: Mapped[float | None] = mapped_column(Float)  # None: the env default
    feedback_origins: Mapped[list] = mapped_column(JSON, default=list)


class EvalSpendDaily(Base):
    """Judge spend per project and UTC day: money settled, and money reserved by calls in flight."""

    __tablename__ = "eval_spend_daily"
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    spent_usd: Mapped[float] = mapped_column(Float, default=0.0)
    reserved_usd: Mapped[float] = mapped_column(Float, default=0.0)


__all__ = [
    "EXECUTION_STATUSES",
    "EvalExecution",
    "EvalRule",
    "EvalSpendDaily",
    "EvalTemplate",
    "EvalTemplateVersion",
    "ProjectEvalSettings",
]
