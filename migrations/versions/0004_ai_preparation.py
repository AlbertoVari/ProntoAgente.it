"""Add governed asynchronous AI preparation.

Revision ID: 0004_ai_preparation
Revises: 0003_order_source_claims
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_ai_preparation"
down_revision: str | None = "0003_order_source_claims"
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
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            "ALTER TABLE runs ADD COLUMN approval_eligible BOOLEAN NOT NULL DEFAULT 1 "
            "CHECK (approval_eligible OR status IN ('proposed', 'rejected'))"
        )
    else:
        op.add_column(
            "runs",
            sa.Column(
                "approval_eligible",
                sa.Boolean(),
                server_default=sa.true(),
                nullable=False,
            ),
        )
        op.create_check_constraint(
            "ck_runs_approval_eligible",
            "runs",
            "approval_eligible OR status IN ('proposed', 'rejected')",
        )

    op.create_table(
        "ai_tenant_policies",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("network_enabled", sa.Boolean(), nullable=False),
        sa.Column("max_input_tokens", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("max_run_microusd", sa.Integer(), nullable=False),
        sa.Column("daily_input_tokens", sa.Integer(), nullable=False),
        sa.Column("daily_output_tokens", sa.Integer(), nullable=False),
        sa.Column("daily_microusd", sa.Integer(), nullable=False),
        sa.Column("lock_version", sa.Integer(), server_default="1", nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "provider IN ('fake', 'openai')", name="ck_ai_policy_provider"
        ),
        sa.CheckConstraint(
            "max_input_tokens > 0 AND max_output_tokens > 0 "
            "AND max_run_microusd >= 0 AND daily_input_tokens > 0 "
            "AND daily_output_tokens > 0 AND daily_microusd >= 0",
            name="ck_ai_policy_limits",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    op.create_table(
        "ai_usage_buckets",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("used_input_tokens", sa.Integer(), nullable=False),
        sa.Column("used_output_tokens", sa.Integer(), nullable=False),
        sa.Column("used_microusd", sa.Integer(), nullable=False),
        sa.Column("reserved_input_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_output_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_microusd", sa.Integer(), nullable=False),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "used_input_tokens >= 0 AND used_output_tokens >= 0 "
            "AND used_microusd >= 0 AND reserved_input_tokens >= 0 "
            "AND reserved_output_tokens >= 0 AND reserved_microusd >= 0",
            name="ck_ai_usage_nonnegative",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tenant_id", "usage_date"),
    )
    op.create_index(
        "ix_ai_usage_buckets_tenant_id_usage_date",
        "ai_usage_buckets",
        ["tenant_id", "usage_date"],
    )

    op.create_table(
        "ai_preparation_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_version_id", sa.String(length=36), nullable=False),
        sa.Column("agent_version_id", sa.String(length=36), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("correlation_id", sa.String(length=36), nullable=False),
        sa.Column("source_ref_hash", sa.String(length=71), nullable=False),
        sa.Column("sealed_input", sa.LargeBinary(), nullable=False),
        sa.Column("encryption_key_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_id", sa.String(length=96), nullable=False),
        sa.Column("prompt_hash", sa.String(length=71), nullable=False),
        sa.Column("tool_name", sa.String(length=96), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("input_token_bound", sa.Integer(), nullable=False),
        sa.Column("reserved_input_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_output_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_microusd", sa.Integer(), nullable=False),
        sa.Column("input_rate_microusd", sa.Integer(), nullable=False),
        sa.Column("output_rate_microusd", sa.Integer(), nullable=False),
        sa.Column("result_run_id", sa.String(length=36), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_microusd", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        _timestamp("created_at"),
        _timestamp("started_at", nullable=True),
        _timestamp("completed_at", nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed', 'unknown')",
            name="ck_ai_preparations_status",
        ),
        sa.CheckConstraint(
            "provider IN ('fake', 'openai')", name="ck_ai_preparations_provider"
        ),
        sa.CheckConstraint(
            "input_token_bound > 0 AND input_token_bound <= reserved_input_tokens "
            "AND reserved_input_tokens > 0 AND reserved_output_tokens > 0 "
            "AND reserved_microusd >= 0",
            name="ck_ai_preparations_reservation",
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
            ["tenant_id", "result_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_version_id"],
            ["workflow_versions.tenant_id", "workflow_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            ["workflows.tenant_id", "workflows.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("correlation_id", name="uq_ai_preparations_correlation"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_ai_preparations_tenant_id_id"
        ),
    )
    op.create_index(
        "ix_ai_preparations_tenant_id_status_created_at",
        "ai_preparation_operations",
        ["tenant_id", "status", "created_at"],
    )

    op.create_table(
        "ai_outbox_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("preparation_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_version", sa.Integer(), server_default="0", nullable=False),
        _timestamp("available_at"),
        _timestamp("lease_until", nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        _timestamp("created_at"),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'retry', 'processed', 'failed', 'unknown')",
            name="ck_ai_outbox_status",
        ),
        sa.CheckConstraint("claim_version >= 0", name="ck_ai_outbox_claim_version"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preparation_id"],
            ["ai_preparation_operations.tenant_id", "ai_preparation_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "preparation_id", name="uq_ai_outbox_preparation"
        ),
    )
    op.create_index(
        "ix_ai_outbox_tenant_id_status_available_at",
        "ai_outbox_events",
        ["tenant_id", "status", "available_at"],
    )

    op.create_table(
        "ai_invocations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("preparation_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_id", sa.String(length=96), nullable=False),
        sa.Column("prompt_hash", sa.String(length=71), nullable=False),
        sa.Column("tool_name", sa.String(length=96), nullable=False),
        sa.Column("input_hash", sa.String(length=71), nullable=False),
        sa.Column("output_hash", sa.String(length=71), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_microusd", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        _timestamp("created_at"),
        _timestamp("completed_at", nullable=True),
        sa.CheckConstraint(
            "status IN ('started', 'completed', 'failed', 'unknown')",
            name="ck_ai_invocations_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preparation_id"],
            ["ai_preparation_operations.tenant_id", "ai_preparation_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "preparation_id", name="uq_ai_invocations_preparation"
        ),
    )
    op.create_index(
        "ix_ai_invocations_tenant_id_created_at",
        "ai_invocations",
        ["tenant_id", "created_at"],
    )

    with op.batch_alter_table("order_source_claims", recreate="always") as batch:
        batch.add_column(
            sa.Column("ai_preparation_id", sa.String(length=36), nullable=True)
        )
        batch.alter_column(
            "run_id", existing_type=sa.String(length=36), nullable=True
        )
        batch.create_foreign_key(
            "fk_order_source_claims_ai_preparation",
            "ai_preparation_operations",
            ["tenant_id", "ai_preparation_id"],
            ["tenant_id", "id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_order_source_claims_ai_preparation",
            ["tenant_id", "ai_preparation_id"],
        )
        batch.create_check_constraint(
            "ck_order_source_claims_resource",
            "run_id IS NOT NULL OR ai_preparation_id IS NOT NULL",
        )

    _replace_run_immutability_trigger(include_approval_eligible=True)


def _replace_run_immutability_trigger(*, include_approval_eligible: bool) -> None:
    dialect = op.get_bind().dialect.name
    columns = (
        "workflow_id, workflow_version_id, agent_version_id, workflow_snapshot, "
        "agent_snapshot, input_payload, proposal, proposal_hash, summary, target, payload"
    )
    if include_approval_eligible:
        columns += ", approval_eligible"
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS runs_immutable_artifacts")
        op.execute(
            f"CREATE TRIGGER runs_immutable_artifacts BEFORE UPDATE OF {columns} ON runs "
            "BEGIN SELECT RAISE(ABORT, 'run proposal and version snapshots are immutable'); END"
        )
    elif dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS runs_immutable_artifacts ON runs")
        op.execute(
            f"CREATE TRIGGER runs_immutable_artifacts BEFORE UPDATE OF {columns} ON runs "
            "FOR EACH ROW EXECUTE FUNCTION pa_reject_immutable()"
        )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    # M3 source reservations without a Run have no representation in 0003.
    # Delete only those AI reservations before restoring run_id NOT NULL.
    op.execute(
        sa.text(
            "DELETE FROM order_source_claims "
            "WHERE run_id IS NULL AND ai_preparation_id IS NOT NULL"
        )
    )
    with op.batch_alter_table("order_source_claims", recreate="always") as batch:
        batch.drop_constraint(
            "ck_order_source_claims_resource", type_="check"
        )
        batch.drop_constraint(
            "uq_order_source_claims_ai_preparation", type_="unique"
        )
        batch.drop_constraint(
            "fk_order_source_claims_ai_preparation", type_="foreignkey"
        )
        batch.alter_column(
            "run_id", existing_type=sa.String(length=36), nullable=False
        )
        batch.drop_column("ai_preparation_id")

    op.drop_index("ix_ai_invocations_tenant_id_created_at", table_name="ai_invocations")
    op.drop_table("ai_invocations")
    op.drop_index(
        "ix_ai_outbox_tenant_id_status_available_at", table_name="ai_outbox_events"
    )
    op.drop_table("ai_outbox_events")
    op.drop_index(
        "ix_ai_preparations_tenant_id_status_created_at",
        table_name="ai_preparation_operations",
    )
    op.drop_table("ai_preparation_operations")
    op.drop_index(
        "ix_ai_usage_buckets_tenant_id_usage_date", table_name="ai_usage_buckets"
    )
    op.drop_table("ai_usage_buckets")
    op.drop_table("ai_tenant_policies")

    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS runs_immutable_artifacts")
        # Modern SQLite can drop this additive column without recreating the
        # populated runs table and temporarily invalidating inbound FKs.
        op.execute("ALTER TABLE runs DROP COLUMN approval_eligible")
    else:
        op.drop_constraint("ck_runs_approval_eligible", "runs", type_="check")
        op.drop_column("runs", "approval_eligible")
    _replace_run_immutability_trigger(include_approval_eligible=False)
