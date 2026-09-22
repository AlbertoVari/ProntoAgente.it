from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from prontoagente.ai import models as _ai_models  # noqa: F401
from prontoagente.db import Base
from prontoagente.v2 import models as _v2_models  # noqa: F401


def test_all_metadata_compiles_for_postgresql() -> None:
    dialect = postgresql.dialect()
    rendered = {
        table.name: str(CreateTable(table).compile(dialect=dialect))
        for table in Base.metadata.sorted_tables
    }
    assert "CREATE TABLE tenants" in rendered["tenants"]
    assert "CREATE TABLE workflow_versions" in rendered["workflow_versions"]
    assert "CREATE TABLE runs" in rendered["runs"]
    assert "CREATE TABLE outbox_events" in rendered["outbox_events"]
    assert "CREATE TABLE order_source_claims" in rendered["order_source_claims"]
    assert "FOREIGN KEY(tenant_id, run_id)" in rendered["order_source_claims"]
    assert "approval_eligible" in rendered["runs"]
    assert "CREATE TABLE ai_tenant_policies" in rendered["ai_tenant_policies"]
    assert "CREATE TABLE ai_preparation_operations" in rendered[
        "ai_preparation_operations"
    ]
    assert "input_token_bound" in rendered["ai_preparation_operations"]
    assert "CREATE TABLE ai_outbox_events" in rendered["ai_outbox_events"]
    assert "claim_version" in rendered["ai_outbox_events"]
    assert "BYTEA" in rendered["ai_preparation_operations"]
    assert "FOREIGN KEY(tenant_id, ai_preparation_id)" in rendered[
        "order_source_claims"
    ]
    assert "JSON" in rendered["runs"]
