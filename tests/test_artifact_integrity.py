from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from prontoagente.connectors import SimulatedConnector
from prontoagente.models import ErpDraftWorkflow, SimulationOperation
from prontoagente.schemas import DryRunRequest
from prontoagente.services import create_dry_run


def request_payload() -> dict[str, Any]:
    return {
        "action_type": "create_sales_order",
        "order": {
            "external_order_id": "IMMUTABLE-1",
            "customer_id": "CUSTOMER-1",
            "currency": "EUR",
            "lines": [{"sku": "SKU-1", "quantity": 1, "unit_price": "1.00"}],
        },
    }


def persisted_draft(session: Session, key: str) -> ErpDraftWorkflow:
    _, body = create_dry_run(
        session,
        request=DryRunRequest.model_validate(request_payload()),
        actor_id="founder",
        idempotency_key=key,
    )
    workflow = session.get(ErpDraftWorkflow, body["id"])
    assert workflow is not None
    return workflow


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("order_payload", {"tampered": True}),
        ("proposal", {"tampered": True}),
        ("proposal_hash", "sha256:" + ("1" * 64)),
    ],
)
def test_proposal_artifacts_reject_orm_updates(
    session_factory: sessionmaker[Session], field: str, replacement: object
) -> None:
    with session_factory() as session:
        workflow = persisted_draft(session, f"orm-immutable-{field}")
        setattr(workflow, field, replacement)
        with pytest.raises(ValueError, match="artifacts are immutable"):
            session.commit()
        session.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE erp_draft_workflows SET order_payload = '{}' WHERE id = :id",
        "UPDATE erp_draft_workflows SET proposal = '{}' WHERE id = :id",
        "UPDATE erp_draft_workflows SET proposal_hash = 'sha256:"
        + ("1" * 64)
        + "' WHERE id = :id",
    ],
    ids=["order-payload", "proposal", "proposal-hash"],
)
def test_proposal_artifacts_reject_raw_sql_updates(
    session_factory: sessionmaker[Session], statement: str
) -> None:
    with session_factory() as session:
        workflow = persisted_draft(session, f"sql-immutable-{uuid4()}")
        with pytest.raises(SQLAlchemyError, match="artifacts are immutable"):
            session.execute(text(statement), {"id": workflow.id})
            session.commit()
        session.rollback()


def insert_tampered_workflow(
    session_factory: sessionmaker[Session], *, status: str
) -> ErpDraftWorkflow:
    workflow = ErpDraftWorkflow(
        id=str(uuid4()),
        action_type="create_sales_order",
        status=status,
        order_payload={"external_order_id": "TAMPERED"},
        proposal={"content": "does-not-match-hash"},
        proposal_hash="sha256:" + ("0" * 64),
        approved_by="reviewer" if status == "approved" else None,
        approved_at=datetime.now(UTC) if status == "approved" else None,
    )
    with session_factory() as session:
        session.add(workflow)
        session.commit()
    return workflow


def test_approval_fails_closed_for_persisted_hash_mismatch(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    workflow = insert_tampered_workflow(session_factory, status="proposed")
    response = client.post(
        f"/v1/erp-drafts/{workflow.id}/approve",
        headers={"Idempotency-Key": "tampered-approval", "X-Actor-Id": "reviewer"},
        json={"proposal_hash": workflow.proposal_hash},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "proposal_integrity_violation"


def test_simulation_claim_fails_closed_without_connector_for_hash_mismatch(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    workflow = insert_tampered_workflow(session_factory, status="approved")
    connector_calls = 0

    def fail_if_called(*_args: object, **_kwargs: object) -> dict[str, Any]:
        nonlocal connector_calls
        connector_calls += 1
        raise AssertionError("connector must not run for a corrupt proposal")

    monkeypatch.setattr(SimulatedConnector, "simulate", fail_if_called)
    response = client.post(
        f"/v1/erp-drafts/{workflow.id}/simulate",
        headers={"Idempotency-Key": "tampered-simulate", "X-Actor-Id": "operator"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "proposal_integrity_violation"
    assert connector_calls == 0

    with session_factory() as session:
        operation_count = session.scalar(select(func.count()).select_from(SimulationOperation))
        assert operation_count == 0
