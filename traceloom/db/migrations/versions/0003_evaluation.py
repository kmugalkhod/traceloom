"""evaluation: score configs, annotation queues, eval tables, score columns

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _utc() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "score_configs",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("data_type", sa.String(16), nullable=False),
        sa.Column("min_value", sa.Float(), nullable=True),
        sa.Column("max_value", sa.Float(), nullable=True),
        sa.Column("categories", sa.JSON(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("public_feedback", sa.Boolean(), nullable=False),
        sa.Column("archived_at", _utc(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.Column("updated_at", _utc(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_score_configs_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_score_configs")),
    )
    with op.batch_alter_table("score_configs", schema=None) as batch_op:
        batch_op.create_index("ix_score_configs_project", ["project_id"], unique=False)
        batch_op.create_index(
            "uq_score_configs_project_name_live",
            ["project_id", "name"],
            unique=True,
            sqlite_where=sa.text("archived_at IS NULL"),
            postgresql_where=sa.text("archived_at IS NULL"),
        )

    op.create_table(
        "annotation_queues",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("config_ids", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_annotation_queues_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_annotation_queues")),
    )
    with op.batch_alter_table("annotation_queues", schema=None) as batch_op:
        batch_op.create_index("ix_annotation_queues_project", ["project_id"], unique=False)

    op.create_table(
        "annotation_items",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("queue_id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("object_type", sa.String(16), nullable=False),
        sa.Column("object_id", sa.String(200), nullable=False),
        sa.Column("trace_id", sa.String(200), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("locked_by", sa.String(32), nullable=True),
        sa.Column("locked_until", _utc(), nullable=True),
        sa.Column("completed_by", sa.String(32), nullable=True),
        sa.Column("completed_at", _utc(), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["queue_id"],
            ["annotation_queues.id"],
            name=op.f("fk_annotation_items_queue_id_annotation_queues"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_annotation_items_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_annotation_items")),
        sa.UniqueConstraint(
            "queue_id", "object_type", "object_id", name=op.f("uq_annotation_items_object")
        ),
    )
    with op.batch_alter_table("annotation_items", schema=None) as batch_op:
        batch_op.create_index(
            "ix_annotation_items_queue_status_created",
            ["queue_id", "status", "created_at"],
            unique=False,
        )

    op.create_table(
        "eval_templates",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", _utc(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_eval_templates_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_templates")),
        sa.UniqueConstraint("project_id", "name", name=op.f("uq_eval_templates_project_name")),
    )
    with op.batch_alter_table("eval_templates", schema=None) as batch_op:
        batch_op.create_index("ix_eval_templates_project", ["project_id"], unique=False)

    op.create_table(
        "eval_template_versions",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("template_id", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("output_schema", sa.JSON(), nullable=False),
        sa.Column("connection_id", sa.String(32), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["eval_templates.id"],
            name=op.f("fk_eval_template_versions_template_id_eval_templates"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_template_versions")),
        sa.UniqueConstraint(
            "template_id", "version", name=op.f("uq_eval_template_versions_version")
        ),
    )

    op.create_table(
        "eval_rules",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("template_id", sa.String(32), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("target", sa.String(16), nullable=False),
        sa.Column("target_name", sa.String(500), nullable=True),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("filter", sa.JSON(), nullable=False),
        sa.Column("sampling_pct", sa.Float(), nullable=False),
        sa.Column("delay_seconds", sa.Integer(), nullable=False),
        sa.Column("mapping", sa.JSON(), nullable=False),
        sa.Column("score_name", sa.String(200), nullable=False),
        sa.Column("config_id", sa.String(32), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.Column("updated_at", _utc(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_eval_rules_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["eval_templates.id"],
            name=op.f("fk_eval_rules_template_id_eval_templates"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_rules")),
    )
    with op.batch_alter_table("eval_rules", schema=None) as batch_op:
        batch_op.create_index("ix_eval_rules_project_enabled", ["project_id", "enabled"])

    op.create_table(
        "eval_executions",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("rule_id", sa.String(32), nullable=True),
        sa.Column("run_item_id", sa.String(32), nullable=True),
        sa.Column("template_version_id", sa.String(32), nullable=False),
        sa.Column("object_type", sa.String(16), nullable=False),
        sa.Column("object_id", sa.String(200), nullable=False),
        sa.Column("trace_id", sa.String(200), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", _utc(), nullable=True),
        sa.Column("started_at", _utc(), nullable=True),
        sa.Column("finished_at", _utc(), nullable=True),
        sa.Column("score_id", sa.String(200), nullable=True),
        sa.Column("judge_trace_id", sa.String(200), nullable=True),
        sa.Column("cost", sa.Float(), nullable=True),
        sa.Column("estimated_cost", sa.Float(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_eval_executions_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["eval_rules.id"],
            name=op.f("fk_eval_executions_rule_id_eval_rules"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["template_version_id"],
            ["eval_template_versions.id"],
            name=op.f("fk_eval_executions_template_version_id_eval_template_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_executions")),
    )
    with op.batch_alter_table("eval_executions", schema=None) as batch_op:
        batch_op.create_index(
            "uq_eval_executions_rule_object",
            ["rule_id", "object_type", "object_id"],
            unique=True,
            sqlite_where=sa.text("rule_id IS NOT NULL"),
            postgresql_where=sa.text("rule_id IS NOT NULL"),
        )
        batch_op.create_index(
            "ix_eval_executions_status_run_after", ["status", "run_after"], unique=False
        )
        batch_op.create_index(
            "ix_eval_executions_project_created", ["project_id", "created_at"], unique=False
        )

    op.create_table(
        "project_eval_settings",
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("daily_cost_cap_usd", sa.Float(), nullable=True),
        sa.Column("feedback_origins", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_eval_settings_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id", name=op.f("pk_project_eval_settings")),
    )

    op.create_table(
        "eval_spend_daily",
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("spent_usd", sa.Float(), nullable=False),
        sa.Column("reserved_usd", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_eval_spend_daily_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id", "day", name=op.f("pk_eval_spend_daily")),
    )

    with op.batch_alter_table("scores", schema=None) as batch_op:
        batch_op.add_column(sa.Column("queue_id", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("execution_id", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("channel", sa.String(32), nullable=True))
        batch_op.create_index(
            "ix_scores_project_name_timestamp", ["project_id", "name", "timestamp"], unique=False
        )

    # Scores written from the UI (they carry an author) are ANNOTATION; the rest stay API.
    op.execute("UPDATE scores SET source = 'ANNOTATION' WHERE author_id IS NOT NULL")


def downgrade() -> None:
    op.drop_table("eval_spend_daily")
    op.drop_table("project_eval_settings")
    op.drop_table("eval_executions")
    op.drop_table("eval_rules")
    op.drop_table("eval_template_versions")
    op.drop_table("eval_templates")

    with op.batch_alter_table("scores", schema=None) as batch_op:
        batch_op.drop_index("ix_scores_project_name_timestamp")
        batch_op.drop_column("channel")
        batch_op.drop_column("execution_id")
        batch_op.drop_column("queue_id")

    with op.batch_alter_table("annotation_items", schema=None) as batch_op:
        batch_op.drop_index("ix_annotation_items_queue_status_created")
    op.drop_table("annotation_items")
    with op.batch_alter_table("annotation_queues", schema=None) as batch_op:
        batch_op.drop_index("ix_annotation_queues_project")
    op.drop_table("annotation_queues")
    with op.batch_alter_table("score_configs", schema=None) as batch_op:
        batch_op.drop_index("uq_score_configs_project_name_live")
        batch_op.drop_index("ix_score_configs_project")
    op.drop_table("score_configs")
