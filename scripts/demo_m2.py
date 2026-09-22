"""Run the Milestone 2 simulated-ERP flow against an initialized local database."""

import os
from pprint import pprint
from uuid import uuid4

from fastapi.testclient import TestClient

from prontoagente.main import app
from prontoagente.worker import process_once


def main() -> None:
    token = os.environ.get("PRONTOAGENTE_API_KEY")
    if not token:
        raise SystemExit("set PRONTOAGENTE_API_KEY to the one-time bootstrap value")
    headers = {"Authorization": f"Bearer {token}"}
    marker = uuid4().hex[:10]
    client = TestClient(app)

    agent = client.post(
        "/v2/agents",
        headers=headers,
        json={"slug": f"demo-agent-{marker}", "name": "Demo ERP agent"},
    )
    agent.raise_for_status()
    agent_version = client.post(
        f"/v2/agents/{agent.json()['id']}/versions",
        headers=headers,
        json={"definition": {"instructions": "Prepare a sales-order proposal"}},
    )
    agent_version.raise_for_status()
    published_agent = client.post(
        f"/v2/agents/{agent.json()['id']}/versions/"
        f"{agent_version.json()['id']}/publish",
        headers=headers,
        json={"lock_version": agent_version.json()["lock_version"]},
    )
    published_agent.raise_for_status()

    workflow = client.post(
        "/v2/workflows",
        headers=headers,
        json={"slug": f"demo-workflow-{marker}", "name": "Demo ERP workflow"},
    )
    workflow.raise_for_status()
    workflow_version = client.post(
        f"/v2/workflows/{workflow.json()['id']}/versions",
        headers=headers,
        json={
            "agent_version_id": published_agent.json()["id"],
            "connector": "simulated_erp",
            "action": "create_sales_order",
            "config": {"target": "demo-erp"},
            "input_schema": {"type": "object", "required": ["order_id"]},
            "approval_required": True,
        },
    )
    workflow_version.raise_for_status()
    published_workflow = client.post(
        f"/v2/workflows/{workflow.json()['id']}/versions/"
        f"{workflow_version.json()['id']}/publish",
        headers=headers,
        json={"lock_version": workflow_version.json()["lock_version"]},
    )
    published_workflow.raise_for_status()

    run_response = client.post(
        f"/v2/workflows/{workflow.json()['id']}/runs/dry-run",
        headers={**headers, "Idempotency-Key": f"demo-dry-{marker}"},
        json={"input": {"order_id": f"ORDER-{marker}", "summary": "Demo order"}},
    )
    run_response.raise_for_status()
    run = run_response.json()

    approval = client.post(
        f"/v2/runs/{run['id']}/approve",
        headers={**headers, "Idempotency-Key": f"demo-approve-{marker}"},
        json={"proposal_hash": run["proposal_hash"]},
    )
    approval.raise_for_status()
    execution = client.post(
        f"/v2/runs/{run['id']}/execute",
        headers={**headers, "Idempotency-Key": f"demo-execute-{marker}"},
    )
    execution.raise_for_status()
    process_once()

    final = client.get(
        f"/v2/execution-operations/{execution.json()['operation_id']}",
        headers=headers,
    )
    final.raise_for_status()
    pprint(final.json(), sort_dicts=False)


if __name__ == "__main__":
    main()
