"""Run the complete local demo against an initialized database."""

from pprint import pprint

from fastapi.testclient import TestClient

from prontoagente.main import app


def main() -> None:
    client = TestClient(app)
    draft_response = client.post(
        "/v1/erp-drafts/dry-run",
        headers={"Idempotency-Key": "demo-draft-0001", "X-Actor-Id": "founder"},
        json={
            "action_type": "create_sales_order",
            "order": {
                "external_order_id": "WEB-1001",
                "customer_id": "CUSTOMER-42",
                "currency": "EUR",
                "lines": [{"sku": "PLAN-PRO", "quantity": 2, "unit_price": "49.90"}],
            },
        },
    )
    draft_response.raise_for_status()
    draft = draft_response.json()

    approval_response = client.post(
        f"/v1/erp-drafts/{draft['id']}/approve",
        headers={"Idempotency-Key": "demo-approve-0001", "X-Actor-Id": "founder"},
        json={"proposal_hash": draft["proposal_hash"]},
    )
    approval_response.raise_for_status()

    simulation_response = client.post(
        f"/v1/erp-drafts/{draft['id']}/simulate",
        headers={"Idempotency-Key": "demo-simulate-0001", "X-Actor-Id": "founder"},
    )
    simulation_response.raise_for_status()
    pprint(simulation_response.json(), sort_dicts=False)


if __name__ == "__main__":
    main()

