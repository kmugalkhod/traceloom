"""Phase 3 dataset models: datasets, versioned items, runs and run items."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from traceloom.db.base import Base, UTCDateTime, new_id, utcnow


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_datasets_project_name"),)


class DatasetItem(Base):
    __tablename__ = "dataset_items"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | archived
    source_trace_id: Mapped[str | None] = mapped_column(String(200))
    source_observation_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_dataset_items_dataset_status", "dataset_id", "status"),)


class DatasetItemVersion(Base):
    """An immutable snapshot of an item, written on every create, edit and archive."""

    __tablename__ = "dataset_item_versions"
    item_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_items.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    input: Mapped[object | None] = mapped_column(JSON)
    expected_output: Mapped[object | None] = mapped_column(JSON)
    metadata_: Mapped[object | None] = mapped_column("metadata", JSON)
    status: Mapped[str] = mapped_column(String(16), default="active")
    actor: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        Index("ix_dataset_item_versions_project_created", "project_id", "created_at"),
    )


class DatasetRun(Base):
    __tablename__ = "dataset_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    dataset_version: Mapped[datetime] = mapped_column(UTCDateTime)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|completed|failed
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (UniqueConstraint("dataset_id", "name", name="uq_dataset_runs_dataset_name"),)


class DatasetRunItem(Base):
    __tablename__ = "dataset_run_items"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("dataset_runs.id", ondelete="CASCADE"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    item_id: Mapped[str] = mapped_column(ForeignKey("dataset_items.id", ondelete="CASCADE"))
    item_version: Mapped[int] = mapped_column(Integer)
    trace_id: Mapped[str | None] = mapped_column(String(200))
    observation_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    run_after: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error: Mapped[str | None] = mapped_column(Text)
    output: Mapped[object | None] = mapped_column(JSON)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    cost: Mapped[float | None] = mapped_column(Float)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        Index("ix_dataset_run_items_run", "run_id"),
        Index("ix_dataset_run_items_status_run_after", "status", "run_after"),
    )
