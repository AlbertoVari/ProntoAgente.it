"""Persistence models and database-level invariants."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
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


def utc_now() -> datetime:
    """Return an aware timestamp in UTC."""

    return datetime.now(UTC)


class ErpDraftWorkflow(Base):
    """A proposed ERP mutation and its explicit approval lifecycle."""

    __tablename__ = "erp_draft_workflows"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('create_sales_order')",
            name="ck_erp_draft_workflows_action_type",
        ),
        CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'simulated')",
            name="ck_erp_draft_workflows_status",
        ),
        CheckConstraint(
            "(status = 'rejected' AND rejected_by IS NOT NULL "
            "AND rejected_at IS NOT NULL AND rejection_reason IS NOT NULL "
            "AND length(rejection_reason) BETWEEN 3 AND 500) "
            "OR (status != 'rejected' AND rejected_by IS NULL "
            "AND rejected_at IS NULL AND rejection_reason IS NULL)",
            name="ck_erp_draft_workflows_rejection_fields",
        ),
        Index("ix_erp_draft_workflows_status", "status"),
        Index("ix_erp_draft_workflows_created_at", "created_at"),
        Index("ix_erp_draft_workflows_tenant_id_id", "tenant_id", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="legacy-local", server_default="legacy-local"
    )
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    order_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    proposal: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    simulation_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __mapper_args__ = {"version_id_col": version_id}


class AuditEvent(Base):
    """Immutable, append-only record of a workflow transition."""

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('draft_proposed', 'proposal_approved', "
            "'proposal_rejected', 'simulation_started', 'simulation_completed', "
            "'simulation_failed')",
            name="ck_audit_events_event_type",
        ),
        Index("ix_audit_events_workflow_id_id", "workflow_id", "id"),
        Index("ix_audit_events_tenant_id_workflow_id", "tenant_id", "workflow_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="legacy-local", server_default="legacy-local"
    )
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("erp_draft_workflows.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class IdempotencyRecord(Base):
    """Persisted response for replay-safe mutation endpoints."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("operation", "idempotency_key", name="uq_idempotency_operation_key"),
        Index("ix_idempotency_records_created_at", "created_at"),
        Index("ix_idempotency_records_tenant_id_created_at", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="legacy-local", server_default="legacy-local"
    )
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    resource_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )


class SimulationOperation(Base):
    """Durable, uniquely claimed execution of one approved ERP proposal."""

    __tablename__ = "simulation_operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('simulating', 'simulated', 'simulation_failed')",
            name="ck_simulation_operations_status",
        ),
        CheckConstraint(
            "connector = 'simulated_erp'",
            name="ck_simulation_operations_connector",
        ),
        CheckConstraint(
            "(status = 'simulating' AND result IS NULL AND error_code IS NULL "
            "AND error_message IS NULL AND completed_at IS NULL) OR "
            "(status = 'simulated' AND result IS NOT NULL AND error_code IS NULL "
            "AND error_message IS NULL AND completed_at IS NOT NULL) OR "
            "(status = 'simulation_failed' AND result IS NULL AND error_code IS NOT NULL "
            "AND error_message IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_simulation_operations_terminal_fields",
        ),
        UniqueConstraint("workflow_id", name="uq_simulation_operations_workflow_id"),
        Index("ix_simulation_operations_status", "status"),
        Index("ix_simulation_operations_tenant_id_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="legacy-local", server_default="legacy-local"
    )
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("erp_draft_workflows.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    connector: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __mapper_args__ = {"version_id_col": version_id}


def _reject_audit_mutation(_mapper: object, _connection: object, _target: AuditEvent) -> None:
    raise ValueError("audit events are append-only")


event.listen(AuditEvent, "before_update", _reject_audit_mutation)
event.listen(AuditEvent, "before_delete", _reject_audit_mutation)


def _reject_artifact_mutation(
    _mapper: object, _connection: object, target: ErpDraftWorkflow
) -> None:
    state = inspect(target)
    immutable_fields = ("order_payload", "proposal", "proposal_hash")
    changed = [name for name in immutable_fields if state.attrs[name].history.has_changes()]
    if changed:
        raise ValueError(f"ERP proposal artifacts are immutable: {', '.join(changed)}")


event.listen(ErpDraftWorkflow, "before_update", _reject_artifact_mutation)

event.listen(
    AuditEvent.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        """
        CREATE TRIGGER audit_events_no_update
        BEFORE UPDATE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit events are append-only');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    ErpDraftWorkflow.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        """
        CREATE TRIGGER erp_draft_workflows_immutable_artifacts
        BEFORE UPDATE OF order_payload, proposal, proposal_hash ON erp_draft_workflows
        BEGIN
            SELECT RAISE(ABORT, 'ERP proposal artifacts are immutable');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    AuditEvent.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        """
        CREATE TRIGGER audit_events_no_delete
        BEFORE DELETE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit events are append-only');
        END
        """
    ).execute_if(dialect="sqlite"),
)
