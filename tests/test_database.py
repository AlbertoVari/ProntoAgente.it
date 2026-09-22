import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from prontoagente.models import AuditEvent


def test_sqlite_foreign_keys_are_enabled_on_every_connection(db_engine: Engine) -> None:
    with db_engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1

    db_engine.dispose()

    with db_engine.connect() as new_connection:
        assert new_connection.scalar(text("PRAGMA foreign_keys")) == 1


def test_sqlite_rejects_orphan_audit_event(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        session.add(
            AuditEvent(
                workflow_id="00000000-0000-0000-0000-000000000000",
                event_type="draft_proposed",
                actor_id="tester",
                idempotency_key="foreign-key-test",
                payload={},
            )
        )
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            session.commit()
