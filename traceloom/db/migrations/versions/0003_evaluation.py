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


_SCORES_INDEX = "ix_scores_project_name_timestamp"
_BACKFILL_BATCH = 5000


def _create_scores_name_index() -> None:
    columns = ["project_id", "name", "timestamp"]
    if op.get_bind().dialect.name == "postgresql":
        # Scores can be large: build without blocking writes (outside the transaction).
        with op.get_context().autocommit_block():
            op.create_index(
                _SCORES_INDEX, "scores", columns, postgresql_concurrently=True, if_not_exists=True
            )
        return
    with op.batch_alter_table("scores", schema=None) as batch_op:
        batch_op.create_index(_SCORES_INDEX, columns, unique=False)


def _drop_scores_name_index() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.drop_index(_SCORES_INDEX, "scores", postgresql_concurrently=True, if_exists=True)
        return
    with op.batch_alter_table("scores", schema=None) as batch_op:
        batch_op.drop_index(_SCORES_INDEX)


def _backfill_annotation_source() -> None:
    """Scores written from the UI (they carry an author) are ANNOTATION; the rest stay API."""
    conn = op.get_bind()
    scores = sa.table(
        "scores",
        sa.column("project_id", sa.String),
        sa.column("id", sa.String),
        sa.column("source", sa.String),
        sa.column("author_id", sa.String),
    )
    while True:
        keys = conn.execute(
            sa.select(scores.c.project_id, scores.c.id)
            .where(scores.c.author_id.is_not(None), scores.c.source != "ANNOTATION")
            .limit(_BACKFILL_BATCH)
        ).all()
        if not keys:
            return
        for project_id, score_id in keys:
            conn.execute(
                sa.update(scores)
                .where(scores.c.project_id == project_id, scores.c.id == score_id)
                .values(source="ANNOTATION")
            )


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
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("template_id", sa.String(32), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("connection_id", sa.String(32), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
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
    _create_scores_name_index()
    _backfill_annotation_source()

    op.create_table(
        "datasets",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.Column("updated_at", _utc(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_datasets_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datasets")),
        sa.UniqueConstraint("project_id", "name", name=op.f("uq_datasets_project_name")),
    )

    op.create_table(
        "dataset_items",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("dataset_id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_trace_id", sa.String(200), nullable=True),
        sa.Column("source_observation_id", sa.String(200), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.Column("updated_at", _utc(), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name=op.f("fk_dataset_items_dataset_id_datasets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_dataset_items_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_items")),
    )
    with op.batch_alter_table("dataset_items", schema=None) as batch_op:
        batch_op.create_index(
            "ix_dataset_items_dataset_status", ["dataset_id", "status"], unique=False
        )

    op.create_table(
        "dataset_item_versions",
        sa.Column("item_id", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("input", sa.JSON(), nullable=True),
        sa.Column("expected_output", sa.JSON(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(64), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["dataset_items.id"],
            name=op.f("fk_dataset_item_versions_item_id_dataset_items"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_dataset_item_versions_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("item_id", "version", name=op.f("pk_dataset_item_versions")),
    )
    with op.batch_alter_table("dataset_item_versions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_dataset_item_versions_project_created",
            ["project_id", "created_at"],
            unique=False,
        )

    op.create_table(
        "dataset_runs",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("dataset_id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("dataset_version", _utc(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.Column("finished_at", _utc(), nullable=True),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name=op.f("fk_dataset_runs_dataset_id_datasets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_dataset_runs_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_runs")),
        sa.UniqueConstraint("dataset_id", "name", name=op.f("uq_dataset_runs_dataset_name")),
    )

    op.create_table(
        "dataset_run_items",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("item_id", sa.String(32), nullable=False),
        sa.Column("item_version", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(200), nullable=True),
        sa.Column("observation_id", sa.String(200), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("run_after", _utc(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("cost", sa.Float(), nullable=True),
        sa.Column("estimated_cost", sa.Float(), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["dataset_runs.id"],
            name=op.f("fk_dataset_run_items_run_id_dataset_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_dataset_run_items_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["dataset_items.id"],
            name=op.f("fk_dataset_run_items_item_id_dataset_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_run_items")),
    )
    with op.batch_alter_table("dataset_run_items", schema=None) as batch_op:
        batch_op.create_index("ix_dataset_run_items_run", ["run_id"], unique=False)
        batch_op.create_index(
            "ix_dataset_run_items_status_run_after", ["status", "run_after"], unique=False
        )


def downgrade() -> None:
    op.drop_table("dataset_run_items")
    op.drop_table("dataset_runs")
    op.drop_table("dataset_item_versions")
    op.drop_table("dataset_items")
    op.drop_table("datasets")
    op.drop_table("eval_spend_daily")
    op.drop_table("project_eval_settings")
    op.drop_table("eval_executions")
    op.drop_table("eval_rules")
    op.drop_table("eval_template_versions")
    op.drop_table("eval_templates")

    _drop_scores_name_index()
    with op.batch_alter_table("scores", schema=None) as batch_op:
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
