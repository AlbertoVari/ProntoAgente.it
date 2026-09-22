"""Add the tenant-scoped Milestone 2 platform.

Revision ID: 0002_milestone2_platform
Revises: 0001_initial
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_milestone2_platform"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp(name: str, *, nullable: bool = False) -> sa.Column[sa.DateTime]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=None if nullable else sa.text("CURRENT_TIMESTAMP"),
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        _timestamp("created_at"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_tenants_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    tenants = sa.table(
        "tenants",
        sa.column("id", sa.String()),
        sa.column("slug", sa.String()),
        sa.column("name", sa.String()),
        sa.column("status", sa.String()),
    )
    op.bulk_insert(
        tenants,
        [
            {
                "id": "legacy-local",
                "slug": "legacy-local",
                "name": "Legacy local",
                "status": "active",
            }
        ],
    )

    op.create_table(
        "principals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=160), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        _timestamp("created_at"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_principals_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_principals_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "subject", name="uq_principals_tenant_subject"),
    )
    op.create_index("ix_principals_tenant_id_status", "principals", ["tenant_id", "status"])

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("prefix", sa.String(length=64), nullable=False),
        sa.Column("secret_digest", sa.String(length=64), nullable=False),
        _timestamp("expires_at", nullable=True),
        _timestamp("revoked_at", nullable=True),
        _timestamp("created_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_api_keys_tenant_id_id"),
    )
    op.create_index(
        "ix_api_keys_tenant_id_principal_id", "api_keys", ["tenant_id", "principal_id"]
    )

    op.create_table(
        "agents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        _timestamp("created_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_agents_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_agents_tenant_slug"),
    )
    op.create_index("ix_agents_tenant_id_created_at", "agents", ["tenant_id", "created_at"])

    op.create_table(
        "agent_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("lock_version", sa.Integer(), server_default="1", nullable=False),
        _timestamp("created_at"),
        _timestamp("published_at", nullable=True),
        sa.CheckConstraint("status IN ('draft', 'published')", name="ck_agent_versions_status"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_agent_versions_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "agent_id", "version", name="uq_agent_versions_number"
        ),
    )
    op.create_index(
        "ix_agent_versions_tenant_id_agent_id", "agent_versions", ["tenant_id", "agent_id"]
    )

    op.create_table(
        "workflows",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        _timestamp("created_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_workflows_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_workflows_tenant_slug"),
    )
    op.create_index(
        "ix_workflows_tenant_id_created_at", "workflows", ["tenant_id", "created_at"]
    )

    op.create_table(
        "workflow_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("agent_version_id", sa.String(length=36), nullable=False),
        sa.Column("connector", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("lock_version", sa.Integer(), server_default="1", nullable=False),
        _timestamp("created_at"),
        _timestamp("published_at", nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'published')", name="ck_workflow_versions_status"
        ),
        sa.CheckConstraint(
            "connector IN ('simulated_erp', 'm365_mail_intake_v1')",
            name="ck_workflow_versions_connector",
        ),
        sa.CheckConstraint(
            "action IN ('create_sales_order', 'list_messages')",
            name="ck_workflow_versions_action",
        ),
        sa.CheckConstraint(
            "approval_required", name="ck_workflow_versions_approval_required"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_version_id"],
            ["agent_versions.tenant_id", "agent_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            ["workflows.tenant_id", "workflows.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_workflow_versions_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "workflow_id", "version", name="uq_workflow_versions_number"
        ),
    )
    op.create_index(
        "ix_workflow_versions_tenant_id_workflow_id",
        "workflow_versions",
        ["tenant_id", "workflow_id"],
    )

    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_version_id", sa.String(length=36), nullable=False),
        sa.Column("agent_version_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_snapshot", sa.JSON(), nullable=False),
        sa.Column("agent_snapshot", sa.JSON(), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("proposal", sa.JSON(), nullable=False),
        sa.Column("proposal_hash", sa.String(length=71), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("target", sa.String(length=500), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("approved_by", sa.String(length=36), nullable=True),
        _timestamp("approved_at", nullable=True),
        sa.Column("rejected_by", sa.String(length=36), nullable=True),
        _timestamp("rejected_at", nullable=True),
        sa.Column("rejection_reason", sa.String(length=500), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'execution_pending', "
            "'executed', 'failed', 'unknown')",
            name="ck_runs_status",
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_version_id"],
            ["agent_versions.tenant_id", "agent_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "approved_by"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "rejected_by"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            ["workflows.tenant_id", "workflows.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_version_id"],
            ["workflow_versions.tenant_id", "workflow_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_runs_tenant_id_id"),
    )
    op.create_index(
        "ix_runs_tenant_id_status_created_at", "runs", ["tenant_id", "status", "created_at"]
    )

    op.create_table(
        "execution_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("connector", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        _timestamp("created_at"),
        _timestamp("completed_at", nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'executing', 'executed', 'failed', 'unknown')",
            name="ck_execution_operations_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"], ["runs.tenant_id", "runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_execution_operations_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "run_id", name="uq_execution_operations_run"),
    )
    op.create_index(
        "ix_execution_operations_tenant_id_status",
        "execution_operations",
        ["tenant_id", "status"],
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        _timestamp("available_at"),
        _timestamp("lease_until", nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        _timestamp("created_at"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'retry', 'processed', 'failed', 'unknown')",
            name="ck_outbox_events_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["execution_operations.tenant_id", "execution_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "operation_id", name="uq_outbox_events_operation"),
    )
    op.create_index(
        "ix_outbox_events_tenant_id_status_available_at",
        "outbox_events",
        ["tenant_id", "status", "available_at"],
    )

    op.create_table(
        "audit_events_v2",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        _timestamp("created_at"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"], ["runs.tenant_id", "runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_events_v2_tenant_id_run_id_id",
        "audit_events_v2",
        ["tenant_id", "run_id", "id"],
    )

    op.create_table(
        "idempotency_records_v2",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=96), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=False),
        sa.Column("request_hash", sa.String(length=71), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=False),
        _timestamp("created_at"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "operation", "idempotency_key", name="uq_idempotency_v2_scope"
        ),
    )
    op.create_index(
        "ix_idempotency_v2_tenant_id_created_at",
        "idempotency_records_v2",
        ["tenant_id", "created_at"],
    )

    for table_name in (
        "erp_draft_workflows",
        "simulation_operations",
        "audit_events",
        "idempotency_records",
    ):
        op.add_column(
            table_name,
            sa.Column(
                "tenant_id",
                sa.String(length=64),
                server_default="legacy-local",
                nullable=False,
            ),
        )
    op.create_index(
        "ix_erp_draft_workflows_tenant_id_id", "erp_draft_workflows", ["tenant_id", "id"]
    )
    op.create_index(
        "ix_simulation_operations_tenant_id_status",
        "simulation_operations",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_audit_events_tenant_id_workflow_id", "audit_events", ["tenant_id", "workflow_id"]
    )
    op.create_index(
        "ix_idempotency_records_tenant_id_created_at",
        "idempotency_records",
        ["tenant_id", "created_at"],
    )

    _create_immutability_triggers()


def _create_immutability_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        statements = (
            """CREATE TRIGGER agent_versions_published_immutable
            BEFORE UPDATE ON agent_versions WHEN OLD.status = 'published'
            BEGIN SELECT RAISE(ABORT, 'published versions are immutable'); END""",
            """CREATE TRIGGER workflow_versions_published_immutable
            BEFORE UPDATE ON workflow_versions WHEN OLD.status = 'published'
            BEGIN SELECT RAISE(ABORT, 'published versions are immutable'); END""",
            """CREATE TRIGGER runs_immutable_artifacts
            BEFORE UPDATE OF workflow_id, workflow_version_id, agent_version_id,
            workflow_snapshot, agent_snapshot, input_payload, proposal, proposal_hash,
            summary, target, payload ON runs
            BEGIN SELECT RAISE(ABORT, 'run proposal and version snapshots are immutable'); END""",
            """CREATE TRIGGER audit_events_v2_no_update
            BEFORE UPDATE ON audit_events_v2
            BEGIN SELECT RAISE(ABORT, 'v2 audit events are append-only'); END""",
            """CREATE TRIGGER audit_events_v2_no_delete
            BEFORE DELETE ON audit_events_v2
            BEGIN SELECT RAISE(ABORT, 'v2 audit events are append-only'); END""",
        )
        for statement in statements:
            op.execute(statement)
        return
    if dialect == "postgresql":
        op.execute(
            """CREATE FUNCTION pa_reject_immutable() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'immutable record'; END; $$ LANGUAGE plpgsql"""
        )
        op.execute(
            """CREATE FUNCTION pa_reject_published() RETURNS trigger AS $$
            BEGIN IF OLD.status = 'published' THEN
            RAISE EXCEPTION 'published versions are immutable'; END IF; RETURN NEW; END;
            $$ LANGUAGE plpgsql"""
        )
        op.execute(
            """CREATE TRIGGER erp_draft_workflows_immutable_artifacts
            BEFORE UPDATE OF order_payload, proposal, proposal_hash ON erp_draft_workflows
            FOR EACH ROW EXECUTE FUNCTION pa_reject_immutable()"""
        )
        op.execute(
            """CREATE TRIGGER audit_events_no_mutation BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION pa_reject_immutable()"""
        )
        op.execute(
            """CREATE TRIGGER agent_versions_published_immutable BEFORE UPDATE ON agent_versions
            FOR EACH ROW EXECUTE FUNCTION pa_reject_published()"""
        )
        op.execute(
            """CREATE TRIGGER workflow_versions_published_immutable
            BEFORE UPDATE ON workflow_versions
            FOR EACH ROW EXECUTE FUNCTION pa_reject_published()"""
        )
        op.execute(
            """CREATE TRIGGER runs_immutable_artifacts BEFORE UPDATE OF workflow_id,
            workflow_version_id, agent_version_id, workflow_snapshot, agent_snapshot,
            input_payload, proposal, proposal_hash, summary, target, payload ON runs
            FOR EACH ROW EXECUTE FUNCTION pa_reject_immutable()"""
        )
        op.execute(
            """CREATE TRIGGER audit_events_v2_no_mutation BEFORE UPDATE OR DELETE ON audit_events_v2
            FOR EACH ROW EXECUTE FUNCTION pa_reject_immutable()"""
        )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for name in (
            "audit_events_v2_no_delete",
            "audit_events_v2_no_update",
            "runs_immutable_artifacts",
            "workflow_versions_published_immutable",
            "agent_versions_published_immutable",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {name}")
    elif dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_events_v2_no_mutation ON audit_events_v2")
        op.execute("DROP TRIGGER IF EXISTS runs_immutable_artifacts ON runs")
        op.execute(
            "DROP TRIGGER IF EXISTS workflow_versions_published_immutable ON workflow_versions"
        )
        op.execute("DROP TRIGGER IF EXISTS agent_versions_published_immutable ON agent_versions")
        op.execute("DROP TRIGGER IF EXISTS audit_events_no_mutation ON audit_events")
        op.execute(
            "DROP TRIGGER IF EXISTS erp_draft_workflows_immutable_artifacts "
            "ON erp_draft_workflows"
        )
        op.execute("DROP FUNCTION IF EXISTS pa_reject_published()")
        op.execute("DROP FUNCTION IF EXISTS pa_reject_immutable()")

    op.drop_index("ix_idempotency_records_tenant_id_created_at", table_name="idempotency_records")
    op.drop_index("ix_audit_events_tenant_id_workflow_id", table_name="audit_events")
    op.drop_index("ix_simulation_operations_tenant_id_status", table_name="simulation_operations")
    op.drop_index("ix_erp_draft_workflows_tenant_id_id", table_name="erp_draft_workflows")
    for table_name in (
        "idempotency_records",
        "audit_events",
        "simulation_operations",
        "erp_draft_workflows",
    ):
        op.drop_column(table_name, "tenant_id")

    op.drop_index("ix_idempotency_v2_tenant_id_created_at", table_name="idempotency_records_v2")
    op.drop_table("idempotency_records_v2")
    op.drop_index("ix_audit_events_v2_tenant_id_run_id_id", table_name="audit_events_v2")
    op.drop_table("audit_events_v2")
    op.drop_index("ix_outbox_events_tenant_id_status_available_at", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_execution_operations_tenant_id_status", table_name="execution_operations")
    op.drop_table("execution_operations")
    op.drop_index("ix_runs_tenant_id_status_created_at", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_workflow_versions_tenant_id_workflow_id", table_name="workflow_versions")
    op.drop_table("workflow_versions")
    op.drop_index("ix_workflows_tenant_id_created_at", table_name="workflows")
    op.drop_table("workflows")
    op.drop_index("ix_agent_versions_tenant_id_agent_id", table_name="agent_versions")
    op.drop_table("agent_versions")
    op.drop_index("ix_agents_tenant_id_created_at", table_name="agents")
    op.drop_table("agents")
    op.drop_index("ix_api_keys_tenant_id_principal_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ix_principals_tenant_id_status", table_name="principals")
    op.drop_table("principals")
    op.drop_table("tenants")
