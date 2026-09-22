"""Add tenant-scoped source claims for the offline order showcase.

Revision ID: 0003_order_source_claims
Revises: 0002_milestone2_platform
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_order_source_claims"
down_revision: str | None = "0002_milestone2_platform"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_source_claims",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_connector", sa.String(length=64), nullable=False),
        sa.Column("source_ref_hash", sa.String(length=71), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_connector = 'demo_mailbox_v1'",
            name="ck_order_source_claims_connector",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "source_connector",
            "source_ref_hash",
            name="uq_order_source_claims_source",
        ),
        sa.UniqueConstraint(
            "tenant_id", "run_id", name="uq_order_source_claims_run"
        ),
    )
    op.create_index(
        "ix_order_source_claims_tenant_id_created_at",
        "order_source_claims",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_order_source_claims_tenant_id_created_at",
        table_name="order_source_claims",
    )
    op.drop_table("order_source_claims")
