from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

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
    assert "JSON" in rendered["runs"]

