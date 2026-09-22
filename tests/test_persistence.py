from collections.abc import Iterator

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from prontoagente.db import get_session
from prontoagente.main import create_app


def app_using(session_factory: sessionmaker[Session]):
    application = create_app()

    def override_session() -> Iterator[Session]:
        with session_factory() as session:
            try:
                yield session
            except Exception:
                session.rollback()
                raise

    application.dependency_overrides[get_session] = override_session
    return application


def test_workflow_persists_across_application_restart(
    session_factory: sessionmaker[Session],
) -> None:
    with TestClient(app_using(session_factory)) as first_process:
        created = first_process.post(
            "/v1/erp-drafts/dry-run",
            headers={
                "Idempotency-Key": "restart-draft-key",
                "X-Actor-Id": "founder",
            },
            json={
                "action_type": "create_sales_order",
                "order": {
                    "external_order_id": "RESTART-1",
                    "customer_id": "CUSTOMER-1",
                    "currency": "EUR",
                    "lines": [
                        {"sku": "SKU-1", "quantity": 1, "unit_price": "10.00"}
                    ],
                },
            },
        )
        assert created.status_code == 201
        workflow = created.json()

    with TestClient(app_using(session_factory)) as restarted_process:
        persisted = restarted_process.get(f"/v1/erp-drafts/{workflow['id']}")

    assert persisted.status_code == 200
    assert persisted.json() == workflow
