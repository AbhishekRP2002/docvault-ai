from datetime import UTC, datetime
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from docvault.db import Base


def new_id() -> str:
    """Generate a UUID4 string for a new persisted record."""
    return str(uuid4())


def now() -> datetime:
    """Return the current timezone-aware UTC time for persisted timestamps."""
    return datetime.now(UTC)


class WorkspaceRevision(Base):
    """One committed change per transaction; the zero row identifies this database epoch."""

    __tablename__ = "workspace_revisions"
    __table_args__ = (
        CheckConstraint(
            "(transaction_id = 0) = (epoch IS NOT NULL)", name="workspace_revision_epoch"
        ),
    )
    transaction_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    epoch: Mapped[str | None] = mapped_column(String(36))


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str]
    current_version_id: Mapped[str | None] = mapped_column(String(36))
    latest_version_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Version(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    version_number: Mapped[int]
    filename: Mapped[str]
    mime_type: Mapped[str]
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_key: Mapped[str]
    status: Mapped[str] = mapped_column(default="queued", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None]
    chunk_count: Mapped[int] = mapped_column(default=0)
    token_count: Mapped[int] = mapped_column(default=0)
    parser: Mapped[str | None]
    embedding_model: Mapped[str | None]
    insight_status: Mapped[str] = mapped_column(default="pending")
    insights: Mapped[dict | None] = mapped_column(JSONB)
    insight_error: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("version_id", "ordinal"),
        Index("ix_chunks_search", "search", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 32, "ef_construction": 200},
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int]
    text: Mapped[str] = mapped_column(Text)
    embedding_text: Mapped[str] = mapped_column(Text)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    location: Mapped[dict] = mapped_column(JSONB)
    token_count: Mapped[int]
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    search: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english'::regconfig, text)", persisted=True)
    )


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str]
    resource_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(default="queued", index=True)
    stage: Mapped[str] = mapped_column(default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    token: Mapped[int] = mapped_column(default=0)
    dispatches: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None]
    error_retryable: Mapped[bool | None]
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    attempt: Mapped[int]
    status: Mapped[str] = mapped_column(default="running")
    stage: Mapped[str] = mapped_column(default="queued")
    error: Mapped[str | None]
    error_code: Mapped[str | None]
    error_retryable: Mapped[bool | None]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobStageRun(Base):
    __tablename__ = "job_stage_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("job_attempts.id"), index=True)
    stage: Mapped[str]
    status: Mapped[str] = mapped_column(default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str]
    signature: Mapped[str] = mapped_column(String(64), unique=True)
    version_ids: Mapped[list[str]] = mapped_column(JSONB)
    options: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(default="pending")
    data: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Chat(Base):
    __tablename__ = "chats"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str]
    version_ids: Mapped[list[str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index(
            "one_active_generation_per_chat",
            "chat_id",
            unique=True,
            postgresql_where=text("role = 'assistant' AND status IN ('pending', 'streaming')"),
        ),
        UniqueConstraint("chat_id", "request_key"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    role: Mapped[str]
    status: Mapped[str] = mapped_column(default="complete")
    content: Mapped[str] = mapped_column(Text, default="")
    suggestions: Mapped[list] = mapped_column(JSONB, default=list)
    citations: Mapped[list] = mapped_column(JSONB, default=list)
    version_ids: Mapped[list] = mapped_column(JSONB, default=list)
    parent_id: Mapped[str | None] = mapped_column(String(36))
    request_key: Mapped[str | None]
    request_hash: Mapped[str | None]
    error: Mapped[str | None]
    outcome: Mapped[str | None]
    rewritten_query: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LLMCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resource_id: Mapped[str | None] = mapped_column(String(36), index=True)
    model: Mapped[str]
    operation: Mapped[str]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cached_tokens: Mapped[int | None]
    cost_usd: Mapped[float | None]
    duration_ms: Mapped[float] = mapped_column(default=0)
    status: Mapped[str]
    request_id: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Idempotency(Base):
    __tablename__ = "idempotency"
    key: Mapped[str] = mapped_column(String(240), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(36))
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Batch(Base):
    __tablename__ = "document_batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    items: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MetricBucket(Base):
    __tablename__ = "metric_buckets"
    key: Mapped[str] = mapped_column(String(240), primary_key=True)
    count: Mapped[int] = mapped_column(default=0)
    total_ms: Mapped[float] = mapped_column(default=0)
    max_ms: Mapped[float] = mapped_column(default=0)
