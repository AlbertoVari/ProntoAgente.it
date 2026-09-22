from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from prontoagente.config import get_settings
from prontoagente.db import build_engine


def test_0003_upgrade_downgrade_upgrade_creates_portable_source_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite:///{tmp_path / 'order-source-migration.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")

    def table_state() -> tuple[bool, set[tuple[str, ...]], list[dict[str, object]]]:
        engine = build_engine(database_url)
        try:
            inspector = inspect(engine)
            if not inspector.has_table("order_source_claims"):
                return False, set(), []
            unique_columns = {
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(
                    "order_source_claims"
                )
            }
            foreign_keys = inspector.get_foreign_keys("order_source_claims")
            return True, unique_columns, foreign_keys
        finally:
            engine.dispose()

    try:
        command.upgrade(config, "0002_milestone2_platform")
        assert table_state()[0] is False

        command.upgrade(config, "head")
        exists, unique_columns, foreign_keys = table_state()
        assert exists
        assert ("tenant_id", "source_connector", "source_ref_hash") in unique_columns
        assert ("tenant_id", "run_id") in unique_columns
        assert any(
            foreign_key["referred_table"] == "runs"
            and foreign_key["constrained_columns"] == ["tenant_id", "run_id"]
            for foreign_key in foreign_keys
        )

        command.downgrade(config, "0002_milestone2_platform")
        assert table_state()[0] is False
        command.upgrade(config, "head")
        assert table_state()[0] is True
    finally:
        get_settings.cache_clear()
