"""Portable SQLAlchemy models for the tenant-scoped v2 platform."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from prontoagente.db import Base
from prontoagente.models import utc_now


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_tenants_status"),
    )


class Principal(Base):
    __tablename__ = "principals"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_principals_status"),
        UniqueConstraint("tenant_id", "subject", name="uq_principals_tenant_subject"),
        UniqueConstraint("tenant_id", "id", name="uq_principals_tenant_id_id"),
        Index("ix_principals_tenant_id_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    subject: Mapped[str] = mapped_column(String(160), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    roles: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_api_keys_tenant_id_id"),
        Index("ix_api_keys_tenant_id_principal_id", "tenant_id", "principal_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(36), nullable=False)
    key_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "slug", name="uq_agents_tenant_slug"),
        UniqueConstraint("tenant_id", "id", name="uq_agents_tenant_id_id"),
        Index("ix_agents_tenant_id_created_at", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'published')", name="ck_agent_versions_status"),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "agent_id", "version", name="uq_agent_versions_number"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_agent_versions_tenant_id_id"),
        Index("ix_agent_versions_tenant_id_agent_id", "tenant_id", "agent_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __mapper_args__ = {"version_id_col": lock_version}


class Workflow(Base):
    __tablename__ = "workflows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "slug", name="uq_workflows_tenant_slug"),
        UniqueConstraint("tenant_id", "id", name="uq_workflows_tenant_id_id"),
        Index("ix_workflows_tenant_id_created_at", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class WorkflowVersion(Base):
    __tablename__ = "workflow_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'published')", name="ck_workflow_versions_status"
        ),
        CheckConstraint(
            "connector IN ('simulated_erp', 'm365_mail_intake_v1')",
            name="ck_workflow_versions_connector",
        ),
        CheckConstraint(
            "action IN ('create_sales_order', 'list_messages')",
            name="ck_workflow_versions_action",
        ),
        CheckConstraint(
            "approval_required", name="ck_workflow_versions_approval_required"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            ["workflows.tenant_id", "workflows.id"],
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
        UniqueConstraint(
            "tenant_id", "workflow_id", "version", name="uq_workflow_versions_number"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_workflow_versions_tenant_id_id"),
        Index("ix_workflow_versions_tenant_id_workflow_id", "tenant_id", "workflow_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(36), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    agent_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    connector: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __mapper_args__ = {"version_id_col": lock_version}


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'execution_pending', "
            "'executed', 'failed', 'unknown')",
            name="ck_runs_status",
        ),
        CheckConstraint(
            "(status = 'proposed' AND approved_by IS NULL AND approved_at IS NULL "
            "AND rejected_by IS NULL AND rejected_at IS NULL AND rejection_reason IS NULL) "
            "OR (status IN ('approved', 'execution_pending', 'executed', 'failed', 'unknown') "
            "AND approved_by IS NOT NULL AND approved_at IS NOT NULL "
            "AND rejected_by IS NULL AND rejected_at IS NULL AND rejection_reason IS NULL) "
            "OR (status = 'rejected' AND approved_by IS NULL AND approved_at IS NULL "
            "AND rejected_by IS NOT NULL AND rejected_at IS NOT NULL "
            "AND rejection_reason IS NOT NULL)",
            name="ck_runs_decision_consistency",
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
            ["tenant_id", "approved_by"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "rejected_by"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_runs_tenant_id_id"),
        Index("ix_runs_tenant_id_status_created_at", "tenant_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workflow_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    agent_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workflow_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    agent_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    proposal: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    target: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class ExecutionOperation(Base):
    __tablename__ = "execution_operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'executing', 'executed', 'failed', 'unknown')",
            name="ck_execution_operations_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "run_id", name="uq_execution_operations_run"),
        UniqueConstraint("tenant_id", "id", name="uq_execution_operations_tenant_id_id"),
        Index("ix_execution_operations_tenant_id_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    connector: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'retry', 'processed', 'failed', 'unknown')",
            name="ck_outbox_events_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["execution_operations.tenant_id", "execution_operations.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "operation_id", name="uq_outbox_events_operation"),
        Index(
            "ix_outbox_events_tenant_id_status_available_at",
            "tenant_id",
            "status",
            "available_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class V2AuditEvent(Base):
    __tablename__ = "audit_events_v2"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
        ),
        Index("ix_audit_events_v2_tenant_id_run_id_id", "tenant_id", "run_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class V2IdempotencyRecord(Base):
    __tablename__ = "idempotency_records_v2"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "operation", "idempotency_key", name="uq_idempotency_v2_scope"
        ),
        Index("ix_idempotency_v2_tenant_id_created_at", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(96), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


def _reject_published_update(_mapper: object, _connection: object, target: object) -> None:
    state = inspect(target)
    if state is None:
        return
    status_history = state.attrs.status.history
    old_status = status_history.deleted[0] if status_history.deleted else state.attrs.status.value
    if old_status == "published":
        raise ValueError("published versions are immutable")


def _reject_run_artifact_update(_mapper: object, _connection: object, target: Run) -> None:
    state = inspect(target)
    immutable = (
        "workflow_id",
        "workflow_version_id",
        "agent_version_id",
        "workflow_snapshot",
        "agent_snapshot",
        "input_payload",
        "proposal",
        "proposal_hash",
        "summary",
        "target",
        "payload",
    )
    if any(state.attrs[name].history.has_changes() for name in immutable):
        raise ValueError("run proposal and version snapshots are immutable")


def _reject_v2_audit_mutation(
    _mapper: object, _connection: object, _target: V2AuditEvent
) -> None:
    raise ValueError("v2 audit events are append-only")


event.listen(AgentVersion, "before_update", _reject_published_update)
event.listen(WorkflowVersion, "before_update", _reject_published_update)
event.listen(Run, "before_update", _reject_run_artifact_update)
event.listen(V2AuditEvent, "before_update", _reject_v2_audit_mutation)
event.listen(V2AuditEvent, "before_delete", _reject_v2_audit_mutation)


def _sqlite_trigger(table: object, ddl: str) -> None:
    event.listen(
        table,
        "after_create",
        DDL(ddl).execute_if(dialect="sqlite"),  # type: ignore[no-untyped-call]
    )


_sqlite_trigger(
    AgentVersion.__table__,
    """
    CREATE TRIGGER agent_versions_published_immutable
    BEFORE UPDATE ON agent_versions WHEN OLD.status = 'published'
    BEGIN SELECT RAISE(ABORT, 'published versions are immutable'); END
    """,
)
_sqlite_trigger(
    WorkflowVersion.__table__,
    """
    CREATE TRIGGER workflow_versions_published_immutable
    BEFORE UPDATE ON workflow_versions WHEN OLD.status = 'published'
    BEGIN SELECT RAISE(ABORT, 'published versions are immutable'); END
    """,
)
_sqlite_trigger(
    Run.__table__,
    """
    CREATE TRIGGER runs_immutable_artifacts
    BEFORE UPDATE OF workflow_id, workflow_version_id, agent_version_id,
    workflow_snapshot, agent_snapshot, input_payload, proposal, proposal_hash,
    summary, target, payload ON runs
    BEGIN SELECT RAISE(ABORT, 'run proposal and version snapshots are immutable'); END
    """,
)
_sqlite_trigger(
    V2AuditEvent.__table__,
    """
    CREATE TRIGGER audit_events_v2_no_update
    BEFORE UPDATE ON audit_events_v2
    BEGIN SELECT RAISE(ABORT, 'v2 audit events are append-only'); END
    """,
)
_sqlite_trigger(
    V2AuditEvent.__table__,
    """
    CREATE TRIGGER audit_events_v2_no_delete
    BEFORE DELETE ON audit_events_v2
    BEGIN SELECT RAISE(ABORT, 'v2 audit events are append-only'); END
    """,
)
