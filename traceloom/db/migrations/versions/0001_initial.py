"""initial

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=True),
        sa.Column("project_id", sa.String(length=32), nullable=True),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=200), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.create_index("ix_audit_log_org_created", ["org_id", "created_at"], unique=False)

    op.create_table(
        "observations",
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=200), nullable=False),
        sa.Column("trace_id", sa.String(length=200), nullable=False),
        sa.Column("parent_observation_id", sa.String(length=200), nullable=True),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completion_start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input", sa.Text(), nullable=True),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("status_message", sa.Text(), nullable=True),
        sa.Column("version", sa.String(length=200), nullable=True),
        sa.Column("environment", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column("model_parameters", sa.JSON(), nullable=False),
        sa.Column("usage_details", sa.JSON(), nullable=False),
        sa.Column("cost_details", sa.JSON(), nullable=False),
        sa.Column("cost_source", sa.String(length=16), nullable=False),
        sa.Column("usage_source", sa.String(length=16), nullable=False),
        sa.Column("prompt_name", sa.String(length=200), nullable=True),
        sa.Column("prompt_version", sa.Integer(), nullable=True),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("project_id", "id", name=op.f("pk_observations")),
    )
    with op.batch_alter_table("observations", schema=None) as batch_op:
        batch_op.create_index(
            "ix_observations_project_start", ["project_id", "start_time"], unique=False
        )
        batch_op.create_index(
            "ix_observations_project_trace", ["project_id", "trace_id"], unique=False
        )

    op.create_table(
        "organizations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
    )
    op.create_table(
        "scores",
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=200), nullable=False),
        sa.Column("trace_id", sa.String(length=200), nullable=True),
        sa.Column("observation_id", sa.String(length=200), nullable=True),
        sa.Column("session_id", sa.String(length=200), nullable=True),
        sa.Column("dataset_run_id", sa.String(length=200), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("string_value", sa.Text(), nullable=True),
        sa.Column("data_type", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("config_id", sa.String(length=200), nullable=True),
        sa.Column("author_id", sa.String(length=32), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("environment", sa.String(length=100), nullable=False),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("project_id", "id", name=op.f("pk_scores")),
    )
    with op.batch_alter_table("scores", schema=None) as batch_op:
        batch_op.create_index("ix_scores_project_trace", ["project_id", "trace_id"], unique=False)

    op.create_table(
        "traces",
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input", sa.Text(), nullable=True),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("user_id", sa.String(length=200), nullable=True),
        sa.Column("session_id", sa.String(length=200), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("release", sa.String(length=200), nullable=True),
        sa.Column("version", sa.String(length=200), nullable=True),
        sa.Column("environment", sa.String(length=100), nullable=False),
        sa.Column("public", sa.Boolean(), nullable=False),
        sa.Column("bookmarked", sa.Boolean(), nullable=False),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False),
        sa.Column("total_cost", sa.Float(), nullable=False),
        sa.Column("max_level", sa.String(length=16), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("project_id", "id", name=op.f("pk_traces")),
    )
    with op.batch_alter_table("traces", schema=None) as batch_op:
        batch_op.create_index(
            "ix_traces_project_env_timestamp",
            ["project_id", "environment", "timestamp"],
            unique=False,
        )
        batch_op.create_index(
            "ix_traces_project_session", ["project_id", "session_id"], unique=False
        )
        batch_op.create_index(
            "ix_traces_project_timestamp_id",
            ["project_id", sa.literal_column("timestamp DESC"), "id"],
            unique=False,
        )
        batch_op.create_index("ix_traces_project_user", ["project_id", "user_id"], unique=False)

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "invites",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_invites_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invites")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invites_token_hash")),
    )
    op.create_table(
        "memberships",
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_memberships_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_memberships_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "org_id", name=op.f("pk_memberships")),
    )
    op.create_table(
        "password_resets",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_password_resets_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_password_resets")),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_projects_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("public_key", sa.String(length=80), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_api_keys_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("public_key", name=op.f("uq_api_keys_public_key")),
        sa.UniqueConstraint("secret_hash", name=op.f("uq_api_keys_secret_hash")),
    )
    op.create_table(
        "comments",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("object_type", sa.String(length=16), nullable=False),
        sa.Column("object_id", sa.String(length=200), nullable=False),
        sa.Column("trace_id", sa.String(length=200), nullable=False),
        sa.Column("author_id", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name=op.f("fk_comments_author_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_comments_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_comments")),
    )
    with op.batch_alter_table("comments", schema=None) as batch_op:
        batch_op.create_index("ix_comments_project_trace", ["project_id", "trace_id"], unique=False)

    op.create_table(
        "model_definitions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=True),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("match_pattern", sa.String(length=500), nullable=False),
        sa.Column("tokenizer", sa.String(length=100), nullable=True),
        sa.Column("prices", sa.JSON(), nullable=False),
        sa.Column("tiers", sa.JSON(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_model_definitions_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_definitions")),
    )
    op.create_table(
        "saved_views",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_saved_views_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_saved_views_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_views")),
    )
    op.create_table(
        "user_prefs",
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_user_prefs_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_prefs_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "project_id", "key", name=op.f("pk_user_prefs")),
    )


def downgrade() -> None:
    op.drop_table("user_prefs")
    op.drop_table("saved_views")
    op.drop_table("model_definitions")
    with op.batch_alter_table("comments", schema=None) as batch_op:
        batch_op.drop_index("ix_comments_project_trace")

    op.drop_table("comments")
    op.drop_table("api_keys")
    op.drop_table("projects")
    op.drop_table("password_resets")
    op.drop_table("memberships")
    op.drop_table("invites")
    op.drop_table("users")
    with op.batch_alter_table("traces", schema=None) as batch_op:
        batch_op.drop_index("ix_traces_project_user")
        batch_op.drop_index("ix_traces_project_timestamp_id")
        batch_op.drop_index("ix_traces_project_session")
        batch_op.drop_index("ix_traces_project_env_timestamp")

    op.drop_table("traces")
    with op.batch_alter_table("scores", schema=None) as batch_op:
        batch_op.drop_index("ix_scores_project_trace")

    op.drop_table("scores")
    op.drop_table("organizations")
    with op.batch_alter_table("observations", schema=None) as batch_op:
        batch_op.drop_index("ix_observations_project_trace")
        batch_op.drop_index("ix_observations_project_start")

    op.drop_table("observations")
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_index("ix_audit_log_org_created")

    op.drop_table("audit_log")
