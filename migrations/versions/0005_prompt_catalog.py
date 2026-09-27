"""Tenant prompt versions and immutable preparation snapshots.

Revision ID: 0005_prompt_catalog
Revises: 0004_ai_preparation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_prompt_catalog"
down_revision: str | None = "0004_ai_preparation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prompts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_prompts_tenant_slug"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_prompts_tenant_id_id"),
    )
    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("prompt_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("system_prompt", sa.String(8192), nullable=False),
        sa.Column("tool_name", sa.String(96), nullable=False),
        sa.Column("prompt_hash", sa.String(71), nullable=True),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('draft', 'published')", name="ck_prompt_versions_status"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "prompt_id"], ["prompts.tenant_id", "prompts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["principals.tenant_id", "principals.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "prompt_id", "version", name="uq_prompt_versions_number"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_prompt_versions_tenant_id_id"),
    )
    op.add_column(
        "ai_preparation_operations", sa.Column("prompt_snapshot", sa.JSON(), nullable=True)
    )
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("""CREATE TRIGGER ai_preparations_immutable_prompt
            BEFORE UPDATE OF prompt_id, prompt_hash, prompt_snapshot, tool_name
            ON ai_preparation_operations
            BEGIN SELECT RAISE(ABORT, 'preparation prompt snapshot is immutable'); END""")
        op.execute("""CREATE TRIGGER prompt_versions_published_immutable
            BEFORE UPDATE ON prompt_versions WHEN OLD.status = 'published'
            BEGIN SELECT RAISE(ABORT, 'published versions are immutable'); END""")
        op.execute("""CREATE TRIGGER prompt_versions_published_no_delete
            BEFORE DELETE ON prompt_versions WHEN OLD.status = 'published'
            BEGIN SELECT RAISE(ABORT, 'published versions are immutable'); END""")
    elif dialect == "postgresql":
        op.execute("""CREATE TRIGGER ai_preparations_immutable_prompt
            BEFORE UPDATE OF prompt_id, prompt_hash, prompt_snapshot, tool_name
            ON ai_preparation_operations FOR EACH ROW
            EXECUTE FUNCTION pa_reject_immutable()""")
        op.execute("""CREATE TRIGGER prompt_versions_published_immutable
            BEFORE UPDATE ON prompt_versions FOR EACH ROW
            EXECUTE FUNCTION pa_reject_published()""")
        op.execute("""CREATE OR REPLACE FUNCTION pa_reject_prompt_delete()
            RETURNS trigger AS $$ BEGIN IF OLD.status = 'published' THEN
            RAISE EXCEPTION 'published versions are immutable'; END IF;
            RETURN OLD; END; $$ LANGUAGE plpgsql""")
        op.execute("""CREATE TRIGGER prompt_versions_published_no_delete
            BEFORE DELETE ON prompt_versions FOR EACH ROW
            EXECUTE FUNCTION pa_reject_prompt_delete()""")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS ai_preparations_immutable_prompt")
        op.execute("DROP TRIGGER IF EXISTS prompt_versions_published_no_delete")
        op.execute("DROP TRIGGER IF EXISTS prompt_versions_published_immutable")
    elif dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS ai_preparations_immutable_prompt ON ai_preparation_operations"
        )
        op.execute("DROP TRIGGER IF EXISTS prompt_versions_published_no_delete ON prompt_versions")
        op.execute("DROP TRIGGER IF EXISTS prompt_versions_published_immutable ON prompt_versions")
        op.execute("DROP FUNCTION pa_reject_prompt_delete()")
    op.drop_column("ai_preparation_operations", "prompt_snapshot")
    op.drop_table("prompt_versions")
    op.drop_table("prompts")
