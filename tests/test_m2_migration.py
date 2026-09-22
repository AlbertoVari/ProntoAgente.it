import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from prontoagente.config import get_settings


def test_populated_m1_upgrade_preserves_data_and_triggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "m1-upgrade.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")

    try:
        command.upgrade(config, "0001_initial")
        connection = sqlite3.connect(database_path)
        proposal = {"schema_version": "1.0", "value": "preserved"}
        proposal_hash = "sha256:" + ("a" * 64)
        connection.execute(
            """INSERT INTO erp_draft_workflows
            (id, version_id, action_type, status, order_payload, proposal, proposal_hash,
             created_at, updated_at)
            VALUES (?, 1, 'create_sales_order', 'proposed', ?, ?, ?, CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP)""",
            ("workflow-1", json.dumps({"order": 1}), json.dumps(proposal), proposal_hash),
        )
        connection.execute(
            """INSERT INTO audit_events
            (workflow_id, event_type, actor_id, idempotency_key, payload, created_at)
            VALUES ('workflow-1', 'draft_proposed', 'founder', 'migration-audit', '{}',
                    CURRENT_TIMESTAMP)"""
        )
        connection.execute(
            """INSERT INTO idempotency_records
            (operation, idempotency_key, actor_id, request_hash, response_status,
             response_body, resource_id, created_at)
            VALUES ('erp_draft.dry_run', 'migration-idempotency', 'founder', ?, 201,
                    ?, 'workflow-1', CURRENT_TIMESTAMP)""",
            (proposal_hash, json.dumps({"id": "workflow-1"})),
        )
        connection.execute(
            """INSERT INTO simulation_operations
            (id, version_id, workflow_id, status, proposal_hash, connector, requested_by,
             idempotency_key, result, created_at, completed_at)
            VALUES ('operation-1', 1, 'workflow-1', 'simulated', ?, 'simulated_erp',
                    'founder', 'migration-simulation', '{}', CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP)""",
            (proposal_hash,),
        )
        connection.commit()
        connection.close()

        command.upgrade(config, "head")
        connection = sqlite3.connect(database_path)
        assert connection.execute("SELECT count(*) FROM erp_draft_workflows").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM idempotency_records").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM simulation_operations").fetchone() == (1,)
        persisted = connection.execute(
            "SELECT proposal, proposal_hash, tenant_id FROM erp_draft_workflows"
        ).fetchone()
        assert persisted == (json.dumps(proposal), proposal_hash, "legacy-local")
        assert connection.execute("SELECT tenant_id FROM audit_events").fetchone() == (
            "legacy-local",
        )
        assert connection.execute(
            "SELECT response_body, tenant_id FROM idempotency_records"
        ).fetchone() == (json.dumps({"id": "workflow-1"}), "legacy-local")

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE audit_events SET actor_id = 'tampered'")
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="artifacts are immutable"):
            connection.execute("UPDATE erp_draft_workflows SET proposal = '{}'")
        connection.rollback()
        connection.close()
    finally:
        get_settings.cache_clear()

