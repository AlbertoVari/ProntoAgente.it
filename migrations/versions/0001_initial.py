"""Create ERP draft, audit and idempotency tables.

Revision ID: 0001_initial
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "erp_draft_workflows",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("version_id", sa.Integer(), server_default="1", nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("order_payload", sa.JSON(), nullable=False),
        sa.Column("proposal", sa.JSON(), nullable=False),
        sa.Column("proposal_hash", sa.String(length=71), nullable=False),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_by", sa.String(length=128), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.String(length=500), nullable=True),
        sa.Column("simulation_result", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action_type IN ('create_sales_order')",
            name="ck_erp_draft_workflows_action_type",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'simulated')",
            name="ck_erp_draft_workflows_status",
        ),
        sa.CheckConstraint(
            "(status = 'rejected' AND rejected_by IS NOT NULL "
            "AND rejected_at IS NOT NULL AND rejection_reason IS NOT NULL "
            "AND length(rejection_reason) BETWEEN 3 AND 500) "
            "OR (status != 'rejected' AND rejected_by IS NULL "
            "AND rejected_at IS NULL AND rejection_reason IS NULL)",
            name="ck_erp_draft_workflows_rejection_fields",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_erp_draft_workflows_created_at", "erp_draft_workflows", ["created_at"]
    )
    op.create_index("ix_erp_draft_workflows_status", "erp_draft_workflows", ["status"])

    op.create_table(
        "simulation_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("version_id", sa.Integer(), server_default="1", nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("proposal_hash", sa.String(length=71), nullable=False),
        sa.Column("connector", sa.String(length=64), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('simulating', 'simulated', 'simulation_failed')",
            name="ck_simulation_operations_status",
        ),
        sa.CheckConstraint(
            "connector = 'simulated_erp'",
            name="ck_simulation_operations_connector",
        ),
        sa.CheckConstraint(
            "(status = 'simulating' AND result IS NULL AND error_code IS NULL "
            "AND error_message IS NULL AND completed_at IS NULL) OR "
            "(status = 'simulated' AND result IS NOT NULL AND error_code IS NULL "
            "AND error_message IS NULL AND completed_at IS NOT NULL) OR "
            "(status = 'simulation_failed' AND result IS NULL AND error_code IS NOT NULL "
            "AND error_message IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_simulation_operations_terminal_fields",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["erp_draft_workflows.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", name="uq_simulation_operations_workflow_id"),
    )
    op.create_index(
        "ix_simulation_operations_status", "simulation_operations", ["status"]
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('draft_proposed', 'proposal_approved', "
            "'proposal_rejected', 'simulation_started', 'simulation_completed', "
            "'simulation_failed')",
            name="ck_audit_events_event_type",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["erp_draft_workflows.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_events_workflow_id_id", "audit_events", ["workflow_id", "id"]
    )

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=71), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation", "idempotency_key", name="uq_idempotency_operation_key"
        ),
    )
    op.create_index(
        "ix_idempotency_records_created_at", "idempotency_records", ["created_at"]
    )

    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            """
            CREATE TRIGGER erp_draft_workflows_immutable_artifacts
            BEFORE UPDATE OF order_payload, proposal, proposal_hash ON erp_draft_workflows
            BEGIN
                SELECT RAISE(ABORT, 'ERP proposal artifacts are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER audit_events_no_update
            BEFORE UPDATE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit events are append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER audit_events_no_delete
            BEFORE DELETE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit events are append-only');
            END
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS audit_events_no_delete")
        op.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
        op.execute("DROP TRIGGER IF EXISTS erp_draft_workflows_immutable_artifacts")
    op.drop_index("ix_idempotency_records_created_at", table_name="idempotency_records")
    op.drop_table("idempotency_records")
    op.drop_index("ix_audit_events_workflow_id_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_simulation_operations_status", table_name="simulation_operations")
    op.drop_table("simulation_operations")
    op.drop_index("ix_erp_draft_workflows_status", table_name="erp_draft_workflows")
    op.drop_index("ix_erp_draft_workflows_created_at", table_name="erp_draft_workflows")
    op.drop_table("erp_draft_workflows")
