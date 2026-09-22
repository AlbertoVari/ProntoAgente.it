from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from prontoagente.models import AuditEvent
from prontoagente.schemas import DryRunRequest
from prontoagente.services import create_dry_run


def request_payload() -> dict[str, Any]:
    return {
        "action_type": "create_sales_order",
        "order": {
            "external_order_id": "AUDIT-1",
            "customer_id": "CUSTOMER-1",
            "currency": "EUR",
            "lines": [{"sku": "SKU-1", "quantity": 1, "unit_price": "1.00"}],
        },
    }


def test_audit_events_reject_orm_updates(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        _, body = create_dry_run(
            session,
            request=DryRunRequest.model_validate(request_payload()),
            actor_id="founder",
            idempotency_key="audit-orm-00001",
        )
        event = session.scalar(select(AuditEvent).where(AuditEvent.workflow_id == body["id"]))
        assert event is not None
        event.actor_id = "tampered"
        with pytest.raises(ValueError, match="append-only"):
            session.commit()
        session.rollback()


def test_audit_events_reject_raw_sql_updates_and_deletes(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        _, body = create_dry_run(
            session,
            request=DryRunRequest.model_validate(request_payload()),
            actor_id="founder",
            idempotency_key="audit-sql-00001",
        )

        with pytest.raises(SQLAlchemyError, match="append-only"):
            session.execute(
                text("UPDATE audit_events SET actor_id = 'tampered' WHERE workflow_id = :id"),
                {"id": body["id"]},
            )
            session.commit()
        session.rollback()

        with pytest.raises(SQLAlchemyError, match="append-only"):
            session.execute(
                text("DELETE FROM audit_events WHERE workflow_id = :id"), {"id": body["id"]}
            )
            session.commit()
        session.rollback()

        remaining = session.scalar(
            select(AuditEvent).where(AuditEvent.workflow_id == body["id"])
        )
        assert remaining is not None
        assert remaining.actor_id == "founder"

