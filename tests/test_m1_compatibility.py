from dataclasses import replace

from fastapi.testclient import TestClient

import prontoagente.main as main_module
from prontoagente.canonical import sha256_digest
from prontoagente.config import get_settings
from prontoagente.schemas import DryRunRequest
from prontoagente.services import build_proposal


def test_m1_proposal_and_hash_golden_contract() -> None:
    request = DryRunRequest.model_validate(
        {
            "action_type": "create_sales_order",
            "order": {
                "external_order_id": "GOLDEN-1",
                "customer_id": "CUSTOMER-1",
                "currency": "eur",
                "lines": [{"sku": "SKU-1", "quantity": 2, "unit_price": "12.30"}],
            },
        }
    )
    proposal = build_proposal(request)
    assert proposal == {
        "schema_version": "1.0",
        "action_type": "create_sales_order",
        "summary": "Create sales order GOLDEN-1 for customer CUSTOMER-1",
        "target": {"connector": "simulated_erp", "resource": "sales_order"},
        "document_type": "sales_order",
        "source": {"external_order_id": "GOLDEN-1"},
        "customer": {"external_id": "CUSTOMER-1"},
        "currency": "EUR",
        "lines": [
            {
                "line_number": 1,
                "sku": "SKU-1",
                "quantity": 2,
                "unit_price": "12.30",
                "line_total": "24.60",
            }
        ],
        "totals": {"net_amount": "24.60"},
        "execution": {"connector": "simulated_erp", "mode": "simulation_only"},
    }
    assert sha256_digest(proposal) == (
        "sha256:c363479e1fd954ebead29ff8c42addbdf3b922e4d16e2f0685b3d6cc6f111383"
    )


def test_m1_response_does_not_expose_legacy_tenant(client: TestClient) -> None:
    response = client.post(
        "/v1/erp-drafts/dry-run",
        headers={"Idempotency-Key": "m1-golden-response", "X-Actor-Id": "founder"},
        json={
            "action_type": "create_sales_order",
            "order": {
                "external_order_id": "GOLDEN-1",
                "customer_id": "CUSTOMER-1",
                "currency": "EUR",
                "lines": [{"sku": "SKU-1", "quantity": 2, "unit_price": "12.30"}],
            },
        },
    )
    assert response.status_code == 201
    assert response.json()["proposal_hash"].endswith(
        "c363479e1fd954ebead29ff8c42addbdf3b922e4d16e2f0685b3d6cc6f111383"
    )
    assert "tenant_id" not in response.json()


def test_v1_is_disabled_by_default_only_in_production(monkeypatch) -> None:
    base = get_settings()
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: replace(base, app_env="production", enable_legacy_v1=False),
    )
    disabled_paths = TestClient(main_module.create_app()).get("/openapi.json").json()["paths"]
    assert not any(path.startswith("/v1/") for path in disabled_paths)
    assert any(path.startswith("/v2/") for path in disabled_paths)

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: replace(base, app_env="production", enable_legacy_v1=True),
    )
    enabled_paths = TestClient(main_module.create_app()).get("/openapi.json").json()["paths"]
    assert any(path.startswith("/v1/") for path in enabled_paths)

