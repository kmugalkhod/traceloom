"""evaluation: score configs, annotation queues, score columns

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
