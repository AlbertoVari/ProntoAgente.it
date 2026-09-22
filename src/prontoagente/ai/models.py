"""Persistence model for asynchronous, budgeted AI preparation."""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from prontoagente.db import Base
from prontoagente.models import utc_now


class AiTenantPolicy(Base):
    __tablename__ = "ai_tenant_policies"
    __table_args__ = (
        CheckConstraint("provider IN ('fake', 'openai')", name="ck_ai_policy_provider"),
        CheckConstraint(
            "max_input_tokens > 0 AND max_output_tokens > 0 "
            "AND max_run_microusd >= 0 AND daily_input_tokens > 0 "
            "AND daily_output_tokens > 0 AND daily_microusd >= 0",
            name="ck_ai_policy_limits",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="fake")
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="fake-pa1-v1")
    network_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_run_microusd: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_microusd: Mapped[int] = mapped_column(Integer, nullable=False)
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __mapper_args__ = {"version_id_col": lock_version}


class AiUsageBucket(Base):
    __tablename__ = "ai_usage_buckets"
    __table_args__ = (
        CheckConstraint(
            "used_input_tokens >= 0 AND used_output_tokens >= 0 "
            "AND used_microusd >= 0 AND reserved_input_tokens >= 0 "
            "AND reserved_output_tokens >= 0 AND reserved_microusd >= 0",
            name="ck_ai_usage_nonnegative",
        ),
        Index("ix_ai_usage_buckets_tenant_id_usage_date", "tenant_id", "usage_date"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    used_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_microusd: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_microusd: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class AiPreparationOperation(Base):
    __tablename__ = "ai_preparation_operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed', 'unknown')",
            name="ck_ai_preparations_status",
        ),
        CheckConstraint(
            "provider IN ('fake', 'openai')", name="ck_ai_preparations_provider"
        ),
        CheckConstraint(
            "input_token_bound > 0 AND input_token_bound <= reserved_input_tokens "
            "AND reserved_input_tokens > 0 AND reserved_output_tokens > 0 "
            "AND reserved_microusd >= 0",
            name="ck_ai_preparations_reservation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            ["workflows.tenant_id", "workflows.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_version_id"],
            ["workflow_versions.tenant_id", "workflow_versions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_version_id"],
            ["agent_versions.tenant_id", "agent_versions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "result_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "id", name="uq_ai_preparations_tenant_id_id"
        ),
        UniqueConstraint("correlation_id", name="uq_ai_preparations_correlation"),
        Index(
            "ix_ai_preparations_tenant_id_status_created_at",
            "tenant_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workflow_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    agent_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_ref_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    sealed_input: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encryption_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_id: Mapped[str] = mapped_column(String(96), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(96), nullable=False)
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    input_token_bound: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_microusd: Mapped[int] = mapped_column(Integer, nullable=False)
    input_rate_microusd: Mapped[int] = mapped_column(Integer, nullable=False)
    output_rate_microusd: Mapped[int] = mapped_column(Integer, nullable=False)
    result_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_microusd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AiOutboxEvent(Base):
    __tablename__ = "ai_outbox_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'processing', 'retry', 'processed', 'failed', 'unknown')",
            name="ck_ai_outbox_status",
        ),
        CheckConstraint("claim_version >= 0", name="ck_ai_outbox_claim_version"),
        ForeignKeyConstraint(
            ["tenant_id", "preparation_id"],
            ["ai_preparation_operations.tenant_id", "ai_preparation_operations.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "preparation_id", name="uq_ai_outbox_preparation"
        ),
        Index(
            "ix_ai_outbox_tenant_id_status_available_at",
            "tenant_id",
            "status",
            "available_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    preparation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    claim_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class AiInvocation(Base):
    __tablename__ = "ai_invocations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('started', 'completed', 'failed', 'unknown')",
            name="ck_ai_invocations_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "preparation_id"],
            ["ai_preparation_operations.tenant_id", "ai_preparation_operations.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "preparation_id", name="uq_ai_invocations_preparation"
        ),
        Index("ix_ai_invocations_tenant_id_created_at", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    preparation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_id: Mapped[str] = mapped_column(String(96), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(96), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    output_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_microusd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
