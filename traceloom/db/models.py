"""Meta tables and the lite trace tables (same columns as the ClickHouse tables)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    insert,
)
from sqlalchemy.orm import Mapped, mapped_column

from traceloom.db.base import Base, UTCDateTime, new_id, utcnow

# --- Meta ---------------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Membership(Base):
    __tablename__ = "memberships"
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    org_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16))


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    public_key: Mapped[str] = mapped_column(String(80), unique=True)
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    may_move_protected_labels: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )


class PasswordReset(Base):
    __tablename__ = "password_resets"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ModelDefinition(Base):
    __tablename__ = "model_definitions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    model_name: Mapped[str] = mapped_column(String(200))
    match_pattern: Mapped[str] = mapped_column(String(500))
    tokenizer: Mapped[str | None] = mapped_column(String(100))
    prices: Mapped[dict] = mapped_column(JSON, default=dict)
    tiers: Mapped[list] = mapped_column(JSON, default=list)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime)
    source: Mapped[str] = mapped_column(String(16), default="user")


class SavedView(Base):
    __tablename__ = "saved_views"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    query: Mapped[str] = mapped_column(Text, default="")


class UserPref(Base):
    __tablename__ = "user_prefs"
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    object_type: Mapped[str] = mapped_column(String(16))  # trace | observation
    object_id: Mapped[str] = mapped_column(String(200))
    trace_id: Mapped[str] = mapped_column(String(200))
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_comments_project_trace", "project_id", "trace_id"),)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str | None] = mapped_column(String(32))
    project_id: Mapped[str | None] = mapped_column(String(32))
    actor_type: Mapped[str] = mapped_column(String(16))  # user | api_key | system
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_audit_log_org_created", "org_id", "created_at"),)


# --- Prompts, LLM connections, webhooks (Phase 2) -------------------------------------------

PRODUCTION_LABEL = "production"


class Prompt(Base):
    __tablename__ = "prompts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(16))  # text | chat
    tags: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_prompts_project_name"),)


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    prompt_id: Mapped[str] = mapped_column(ForeignKey("prompts.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[object] = mapped_column(JSON)  # str (text) or list of messages (chat)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    commit_message: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    dependencies: Mapped[list] = mapped_column(JSON, default=list)

    __table_args__ = (
        UniqueConstraint("prompt_id", "version", name="uq_prompt_versions_prompt_version"),
        Index("ix_prompt_versions_project_prompt_version", "project_id", "prompt_id", "version"),
    )


class PromptLabel(Base):
    __tablename__ = "prompt_labels"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    prompt_id: Mapped[str] = mapped_column(
        ForeignKey("prompts.id", ondelete="CASCADE"), primary_key=True
    )
    label: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ProtectedLabel(Base):
    __tablename__ = "protected_labels"
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    label: Mapped[str] = mapped_column(String(36), primary_key=True)


@event.listens_for(Project, "after_insert")
def _protect_production(mapper, connection, project: Project) -> None:
    """Every new project starts with `production` protected (FR-P4)."""
    connection.execute(insert(ProtectedLabel).values(project_id=project.id, label=PRODUCTION_LABEL))


class LLMConnection(Base):
    __tablename__ = "llm_connections"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str | None] = mapped_column(String(500))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    secret_ciphertext: Mapped[str] = mapped_column(Text, default="")
    secret_hint: Mapped[str] = mapped_column(String(16), default="")
    custom_models: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_llm_connections_project", "project_id"),)


class Webhook(Base):
    __tablename__ = "webhooks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(String(2000))
    secret_ciphertext: Mapped[str] = mapped_column(Text, default="")
    events: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_webhooks_project", "project_id"),)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    webhook_id: Mapped[str] = mapped_column(ForeignKey("webhooks.id", ondelete="CASCADE"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    event: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|succeeded|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        Index("ix_webhook_deliveries_webhook_created", "webhook_id", "created_at"),
        Index("ix_webhook_deliveries_status_next", "status", "next_attempt_at"),
    )


# --- Lite trace tables (project_id is part of every key and every index) --------------------


class Trace(Base):
    __tablename__ = "traces"
    project_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(500))
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime)
    input: Mapped[str | None] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[str | None] = mapped_column(String(200))
    session_id: Mapped[str | None] = mapped_column(String(200))
    tags: Mapped[list] = mapped_column(JSON, default=list)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    release: Mapped[str | None] = mapped_column(String(200))
    version: Mapped[str | None] = mapped_column(String(200))
    environment: Mapped[str] = mapped_column(String(100), default="default")
    public: Mapped[bool] = mapped_column(Boolean, default=False)
    bookmarked: Mapped[bool] = mapped_column(Boolean, default=False)
    event_ts: Mapped[datetime | None] = mapped_column(UTCDateTime)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    total_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    total_cost: Mapped[float] = mapped_column(Float, default=0.0)
    max_level: Mapped[str] = mapped_column(String(16), default="DEFAULT")
    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    search_text: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        Index("ix_traces_project_timestamp_id", "project_id", timestamp.desc(), "id"),
        Index("ix_traces_project_session", "project_id", "session_id"),
        Index("ix_traces_project_user", "project_id", "user_id"),
        Index("ix_traces_project_env_timestamp", "project_id", "environment", "timestamp"),
    )


class Observation(Base):
    __tablename__ = "observations"
    project_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(200))
    parent_observation_id: Mapped[str | None] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(32), default="span")
    name: Mapped[str | None] = mapped_column(String(500))
    start_time: Mapped[datetime] = mapped_column(UTCDateTime)
    end_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completion_start_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    input: Mapped[str | None] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    level: Mapped[str] = mapped_column(String(16), default="DEFAULT")
    status_message: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(String(200))
    environment: Mapped[str] = mapped_column(String(100), default="default")
    model: Mapped[str | None] = mapped_column(String(200))
    model_parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    usage_details: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_details: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_source: Mapped[str] = mapped_column(String(16), default="none")
    usage_source: Mapped[str] = mapped_column(String(16), default="none")
    prompt_name: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[int | None] = mapped_column(Integer)
    event_ts: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        Index("ix_observations_project_trace", "project_id", "trace_id"),
        Index("ix_observations_project_start", "project_id", "start_time"),
        Index("ix_observations_project_prompt", "project_id", "prompt_name", "prompt_version"),
    )


class Score(Base):
    __tablename__ = "scores"
    project_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    trace_id: Mapped[str | None] = mapped_column(String(200))
    observation_id: Mapped[str | None] = mapped_column(String(200))
    session_id: Mapped[str | None] = mapped_column(String(200))
    dataset_run_id: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    value: Mapped[float | None] = mapped_column(Float)
    string_value: Mapped[str | None] = mapped_column(Text)
    data_type: Mapped[str] = mapped_column(String(16), default="NUMERIC")
    comment: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="API")
    config_id: Mapped[str | None] = mapped_column(String(200))
    author_id: Mapped[str | None] = mapped_column(String(32))
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime)
    environment: Mapped[str] = mapped_column(String(100), default="default")
    event_ts: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (Index("ix_scores_project_trace", "project_id", "trace_id"),)


__all__ = [
    "ApiKey",
    "AuditLog",
    "Comment",
    "Invite",
    "LLMConnection",
    "Membership",
    "ModelDefinition",
    "Observation",
    "Organization",
    "PasswordReset",
    "Project",
    "Prompt",
    "PromptLabel",
    "PromptVersion",
    "ProtectedLabel",
    "SavedView",
    "Score",
    "Trace",
    "User",
    "UserPref",
    "Webhook",
    "WebhookDelivery",
]
