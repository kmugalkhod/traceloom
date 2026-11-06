"""prompts, labels, LLM connections and webhooks

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _utc() -> sa.DateTime:
    return sa.DateTime(timezone=True)


_INDEX = "ix_observations_project_prompt"


def _create_observations_prompt_index() -> None:
    columns = ["project_id", "prompt_name", "prompt_version"]
    if op.get_bind().dialect.name == "postgresql":
        # Observations can be large: build without blocking writes (outside the transaction).
        with op.get_context().autocommit_block():
            op.create_index(
                _INDEX,
                "observations",
                columns,
                postgresql_concurrently=True,
                if_not_exists=True,
            )
        return
    with op.batch_alter_table("observations", schema=None) as batch_op:
        batch_op.create_index(_INDEX, columns, unique=False)


def _drop_observations_prompt_index() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.drop_index(_INDEX, "observations", postgresql_concurrently=True, if_exists=True)
        return
    with op.batch_alter_table("observations", schema=None) as batch_op:
        batch_op.drop_index(_INDEX)


def upgrade() -> None:
    op.create_table(
        "prompts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.Column("updated_at", _utc(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_prompts_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompts")),
        sa.UniqueConstraint("project_id", "name", name="uq_prompts_project_name"),
    )
    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("prompt_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("commit_message", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.Column("dependencies", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_prompt_versions_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_id"],
            ["prompts.id"],
            name=op.f("fk_prompt_versions_prompt_id_prompts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_versions")),
        sa.UniqueConstraint("prompt_id", "version", name="uq_prompt_versions_prompt_version"),
    )
    with op.batch_alter_table("prompt_versions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_prompt_versions_project_prompt_version",
            ["project_id", "prompt_id", "version"],
            unique=False,
        )
    op.create_table(
        "prompt_labels",
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("prompt_id", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column("updated_at", _utc(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_prompt_labels_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_id"],
            ["prompts.id"],
            name=op.f("fk_prompt_labels_prompt_id_prompts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("prompt_id", "label", name=op.f("pk_prompt_labels")),
    )
    op.create_table(
        "protected_labels",
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_protected_labels_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id", "label", name=op.f("pk_protected_labels")),
    )
    op.create_table(
        "llm_connections",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("secret_hint", sa.String(length=16), nullable=False),
        sa.Column("custom_models", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.Column("updated_at", _utc(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_llm_connections_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_connections")),
    )
    with op.batch_alter_table("llm_connections", schema=None) as batch_op:
        batch_op.create_index("ix_llm_connections_project", ["project_id"], unique=False)
    op.create_table(
        "webhooks",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_webhooks_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhooks")),
    )
    with op.batch_alter_table("webhooks", schema=None) as batch_op:
        batch_op.create_index("ix_webhooks_project", ["project_id"], unique=False)
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("webhook_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", _utc(), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", _utc(), nullable=False),
        sa.Column("delivered_at", _utc(), nullable=True),
        sa.ForeignKeyConstraint(
            ["webhook_id"],
            ["webhooks.id"],
            name=op.f("fk_webhook_deliveries_webhook_id_webhooks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_webhook_deliveries_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_deliveries")),
    )
    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.create_index(
            "ix_webhook_deliveries_webhook_created", ["webhook_id", "created_at"], unique=False
        )
        batch_op.create_index(
            "ix_webhook_deliveries_status_next", ["status", "next_attempt_at"], unique=False
        )

    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "may_move_protected_labels",
                sa.Boolean(),
                nullable=False,
                server_default="0",
            )
        )
    _create_observations_prompt_index()

    # Existing projects start with `production` protected, like new ones.
    op.execute(
        "INSERT INTO protected_labels (project_id, label) SELECT id, 'production' FROM projects"
    )


def downgrade() -> None:
    _drop_observations_prompt_index()
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.drop_column("may_move_protected_labels")

    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.drop_index("ix_webhook_deliveries_status_next")
        batch_op.drop_index("ix_webhook_deliveries_webhook_created")
    op.drop_table("webhook_deliveries")
    with op.batch_alter_table("webhooks", schema=None) as batch_op:
        batch_op.drop_index("ix_webhooks_project")
    op.drop_table("webhooks")
    with op.batch_alter_table("llm_connections", schema=None) as batch_op:
        batch_op.drop_index("ix_llm_connections_project")
    op.drop_table("llm_connections")
    op.drop_table("protected_labels")
    op.drop_table("prompt_labels")
    with op.batch_alter_table("prompt_versions", schema=None) as batch_op:
        batch_op.drop_index("ix_prompt_versions_project_prompt_version")
    op.drop_table("prompt_versions")
    op.drop_table("prompts")
