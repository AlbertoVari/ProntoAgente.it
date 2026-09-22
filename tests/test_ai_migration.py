import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from prontoagente.config import get_settings
from prontoagente.db import build_engine


def test_populated_0003_upgrade_preserves_run_and_source_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "m3-populated-upgrade.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "0003_order_source_claims")
        connection = sqlite3.connect(database_path)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """INSERT INTO principals
            (id, tenant_id, subject, display_name, roles, status, created_at)
            VALUES ('principal-m3', 'legacy-local', 'm3@example.test', 'M3', '["owner"]',
                    'active', CURRENT_TIMESTAMP)"""
        )
        connection.execute(
            """INSERT INTO agents
            (id, tenant_id, slug, name, description, created_by, created_at)
            VALUES ('agent-m3', 'legacy-local', 'agent-m3', 'Agent M3', '', 'principal-m3',
                    CURRENT_TIMESTAMP)"""
        )
        connection.execute(
            """INSERT INTO agent_versions
            (id, tenant_id, agent_id, version, status, definition, created_by, lock_version,
             created_at, published_at)
            VALUES ('agent-version-m3', 'legacy-local', 'agent-m3', 1, 'published', '{}',
                    'principal-m3', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""
        )
        connection.execute(
            """INSERT INTO workflows
            (id, tenant_id, slug, name, description, created_by, created_at)
            VALUES ('workflow-m3', 'legacy-local', 'workflow-m3', 'Workflow M3', '',
                    'principal-m3', CURRENT_TIMESTAMP)"""
        )
        connection.execute(
            """INSERT INTO workflow_versions
            (id, tenant_id, workflow_id, version, status, agent_version_id, connector,
             action, config, input_schema, approval_required, created_by, lock_version,
             created_at, published_at)
            VALUES ('workflow-version-m3', 'legacy-local', 'workflow-m3', 1, 'published',
                    'agent-version-m3', 'simulated_erp', 'create_sales_order', '{}', '{}', 1,
                    'principal-m3', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""
        )
        proposal = {"schema_version": "2.0", "preserved": True}
        proposal_hash = "sha256:" + ("b" * 64)
        connection.execute(
            """INSERT INTO runs
            (id, tenant_id, workflow_id, workflow_version_id, agent_version_id,
             workflow_snapshot, agent_snapshot, input_payload, proposal, proposal_hash,
             summary, target, payload, status, created_by, approved_by, approved_at,
             rejected_by, rejected_at, rejection_reason, created_at, updated_at)
            VALUES ('run-m3', 'legacy-local', 'workflow-m3', 'workflow-version-m3',
                    'agent-version-m3', '{}', '{}', '{}', ?, ?, 'Preserved', 'demo', '{}',
                    'proposed', 'principal-m3', NULL, NULL, NULL, NULL, NULL,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (json.dumps(proposal), proposal_hash),
        )
        connection.execute(
            """INSERT INTO order_source_claims
            (id, tenant_id, source_connector, source_ref_hash, run_id, created_at)
            VALUES ('claim-m3', 'legacy-local', 'demo_mailbox_v1', ?, 'run-m3',
                    CURRENT_TIMESTAMP)""",
            ("sha256:" + ("c" * 64),),
        )
        connection.commit()
        connection.close()

        command.upgrade(config, "head")
        connection = sqlite3.connect(database_path)
        run = connection.execute(
            "SELECT proposal, proposal_hash, approval_eligible FROM runs WHERE id='run-m3'"
        ).fetchone()
        assert run == (json.dumps(proposal), proposal_hash, 1)
        claim = connection.execute(
            "SELECT run_id, ai_preparation_id FROM order_source_claims WHERE id='claim-m3'"
        ).fetchone()
        assert claim == ("run-m3", None)
        assert connection.execute(
            "SELECT count(*) FROM ai_preparation_operations"
        ).fetchone() == (0,)
        connection.close()

        engine = build_engine(database_url)
        try:
            inspector = inspect(engine)
            assert {
                "ai_tenant_policies",
                "ai_usage_buckets",
                "ai_preparation_operations",
                "ai_outbox_events",
                "ai_invocations",
            }.issubset(set(inspector.get_table_names()))
            source_fks = inspector.get_foreign_keys("order_source_claims")
            assert any(
                item["referred_table"] == "ai_preparation_operations"
                and item["constrained_columns"]
                == ["tenant_id", "ai_preparation_id"]
                for item in source_fks
            )
        finally:
            engine.dispose()

        connection = sqlite3.connect(database_path)
        connection.execute("PRAGMA foreign_keys=ON")
        for index, status in enumerate(("queued", "failed", "unknown"), start=1):
            operation_id = f"ai-operation-{status}"
            connection.execute(
                """INSERT INTO ai_preparation_operations
                (id, tenant_id, workflow_id, workflow_version_id, agent_version_id,
                 created_by, status, correlation_id, source_ref_hash, sealed_input,
                 encryption_key_id, provider, model, prompt_id, prompt_hash, tool_name,
                 usage_date, input_token_bound, reserved_input_tokens,
                 reserved_output_tokens, reserved_microusd, input_rate_microusd,
                 output_rate_microusd, created_at)
                VALUES (?, 'legacy-local', 'workflow-m3', 'workflow-version-m3',
                        'agent-version-m3', 'principal-m3', ?, ?, ?, X'01', 'key-v1',
                        'fake', 'fake-pa1-v1', 'email_order_extract/v1', ?,
                        'demo_erp_reconcile_v1', DATE('now'), 2000, 4096, 512, 0, 0, 0,
                        CURRENT_TIMESTAMP)""",
                (
                    operation_id,
                    status,
                    f"00000000-0000-0000-0000-00000000000{index}",
                    "sha256:" + str(index) * 64,
                    "sha256:" + str(index + 3) * 64,
                ),
            )
            connection.execute(
                """INSERT INTO order_source_claims
                (id, tenant_id, source_connector, source_ref_hash, run_id,
                 ai_preparation_id, created_at)
                VALUES (?, 'legacy-local', 'demo_mailbox_v1', ?, NULL, ?,
                        CURRENT_TIMESTAMP)""",
                (
                    f"ai-claim-{status}",
                    "sha256:" + str(index + 6) * 64,
                    operation_id,
                ),
            )
        connection.commit()
        connection.close()

        command.downgrade(config, "0003_order_source_claims")
        connection = sqlite3.connect(database_path)
        assert connection.execute(
            "SELECT count(*) FROM order_source_claims WHERE id='claim-m3'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM order_source_claims WHERE id LIKE 'ai-claim-%'"
        ).fetchone() == (0,)
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(order_source_claims)")
        }
        assert "ai_preparation_id" not in columns
        connection.close()

        command.upgrade(config, "head")
        connection = sqlite3.connect(database_path)
        assert connection.execute(
            "SELECT count(*) FROM order_source_claims WHERE id='claim-m3'"
        ).fetchone() == (1,)
        connection.close()
    finally:
        get_settings.cache_clear()
